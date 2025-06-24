import logging
from guardin_mind.manager import ConfigRead
from concurrent_log_handler import ConcurrentRotatingFileHandler
from selenium import webdriver
from selenium.webdriver import EdgeOptions
from selenium.webdriver.edge.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (ElementNotInteractableException, WebDriverException, TimeoutException)
from webdriver_manager.microsoft import EdgeChromiumDriverManager
from threading import Timer
import threading
import functools
from colorama import Fore, Style, init
import time
from typing import List, Optional
import pathlib
from pydantic import validate_arguments

button_start_stop_rec = ("id", "recbtn")  # Кнопка для начала/остановки записи
recognized_text_field = ("xpath", "//textarea[@id='docel']")  # Поле хранящее распознанный текст

def driver_refresh(func=None, *, max_retries=3):
    def decorator(inner_func):
        @functools.wraps(inner_func)
        def wrapper(self, *args, **kwargs):
            retries = 0
            while retries < max_retries:
                try:
                    return inner_func(self, *args, **kwargs)
                except WebDriverException as e:
                    if 'disconnected' in str(e).lower():
                        retries += 1
                        print(f'WebDriver timed out. Refreshing page (attempt {retries}/{max_retries})')
                        self.driver.refresh()
                        time.sleep(1)
                    else:
                        raise e
            raise WebDriverException(f"Failed after {max_retries} refresh attempts")
        return wrapper

    if func:
        return decorator(func)
    return decorator

def create_stealth_driver(headless):
    options = EdgeOptions()
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/114.0.0.0 Safari/537.36")
    options.add_argument("--autoplay-policy=no-user-gesture-required")
    options.add_argument("--disable-notifications")
    options.add_experimental_option("prefs", {
        "profile.default_content_setting_values.media_stream_mic": 1,
        "profile.default_content_setting_values.media_stream_camera": 1,
    })
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-dev-shm-usage")  # Полезно для Docker/Linux
    options.add_argument("--no-sandbox")  # Для работы под root (например, в Docker)
    options.add_argument("--disable-gpu")  # Иногда помогает в headless-режиме
    options.add_argument("--remote-debugging-port=0")  # Закрывает удалённый дебаггинг
    if headless:
        options.add_argument("--headless")

    # Отключаем логи Selenium (можно добавить при необходимости)
    options.add_experimental_option("excludeSwitches", ["enable-logging"])

    service = Service(EdgeChromiumDriverManager().install())

    driver = webdriver.Edge(service=service, options=options)

    return driver

class OrbitSRSpeechPad:
    def __init__(
        self,
        local_speechpad: bool = False, # В случае True, использует локальный speechpad, по пути
        headless: bool = True
    ):
        # Чтение конфигурации майндера
        ConfigRead(self)
        
        # Конфигурационные параметры
        self.assistant_names: list = [] # Список имен ассистента для вызова
        self.require_assistant_name: bool = False # Требуется ли имя ассистента для активации
        
        # Системные параметры
        self.activation_required: bool = self.require_assistant_name
        self.inactivity_timer = None
        self.recognized_text: str | None = None
        self._recognize_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Настройка логгера
        handler = ConcurrentRotatingFileHandler(
            "orbit-temp/orbit-logs/orbit.log", maxBytes=10**6, backupCount=5, encoding="utf-8"
        )
        logging.basicConfig(
            level=logging.INFO,
            handlers=[handler],
            format='%(asctime)s - %(levelname)s - %(pathname)s - %(message)s'
        )

        # Инициализация драйвера
        self.driver = create_stealth_driver(headless)
        if not local_speechpad:
            self.driver.get("https://speechpad.ru/")
        else:
            # Получаем абсолютный путь к директории текущего скрипта
            script_dir = pathlib.Path(__file__).parent.absolute()
            
            # Формируем путь к index.html относительно директории скрипта
            html_path = script_dir / "speechpad" / "index.html"
            
            # Преобразуем путь в формат file:/// с правильными слешами
            file_url = html_path.as_uri()
            
            self.driver.get(file_url)


        # Ожидание доступа к микрофону
        while True:
            try:
                status = self.driver.execute_script(
                    "return navigator.mediaDevices.getUserMedia({audio: true})"
                    ".then(stream => stream.active ? 'granted' : 'denied')"
                    ".catch(() => 'denied')"
                )
                if "granted" in status:
                    break
            except TimeoutException:
                pass
            
            logging.info("Состояние доступа к микрофону: disabled")
            init()
            print(Fore.CYAN + "[WARNING] Вам необходимо вручную дать доступ к микрофону." + Style.RESET_ALL)
            input(Fore.CYAN + "Нажмите ENTER когда вы дадите доступ к микрофону...")

        # Настройка чекбоксов
        def set_checkbox(driver, checkbox_id: str, action: bool = True):
            checkbox = WebDriverWait(driver, 9).until(
                EC.presence_of_element_located((By.ID, checkbox_id))
            )
            if action != checkbox.is_selected():
                checkbox.click()

        if not local_speechpad:
            try:
                set_checkbox(self.driver, "chkcom", False)
                set_checkbox(self.driver, "chkpunct", False)
                set_checkbox(self.driver, "chkcap", True)
                set_checkbox(self.driver, "chksimple", True)
            except Exception as e:
                logging.critical(f"Не удалось переключить чекбокс: {e}")
                raise ElementNotInteractableException()

        self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")

    def __is_recording_active(self):
        button = self.driver.find_element(*button_start_stop_rec)
        return "255, 165, 0" in button.value_of_css_property("background-color")

    def start_record(self) -> bool | None:
        if not self.__is_recording_active():
            self.driver.find_element(*button_start_stop_rec).click()
            return True
        logging.warning("Запись уже запущена")
        return False

    def stop_record(self) -> bool | None:
        if self.__is_recording_active():
            self.driver.find_element(*button_start_stop_rec).click()
            return True
        logging.warning("Запись уже остановлена")
        return False
    
    def __reset_inactivity_timer(self):
        """Сбрасывает таймер неактивности (если требуется имя ассистента)"""
        if not self.require_assistant_name:
            return
            
        if self.inactivity_timer:
            self.inactivity_timer.cancel()
        
        self.inactivity_timer = Timer(6.0, self.__on_inactivity_timeout)
        self.inactivity_timer.start()

    def __on_inactivity_timeout(self):
        """Обработчик таймаута неактивности"""
        if self.require_assistant_name:
            self.activation_required = True
            print("Таймер истёк. Требуется имя ассистента.")

    def __find_after_any_keyword(self, text: str, keywords: List[str]) -> str | None:
        for keyword in keywords:
            if (index := text.lower().find(keyword.lower())) != -1:
                return text[index:]
        return None

    @validate_arguments
    def start_recognize(self, recognized_refresh_rate: float = 0.05):
        if self._recognize_thread and self._recognize_thread.is_alive():
            raise RuntimeError("Распознавание уже запущено!")

        self._stop_event.clear()
        self._recognize_thread = threading.Thread(
            target=self._recognize_loop,
            args=(recognized_refresh_rate,),
            daemon=True
        )
        self._recognize_thread.start()

    @driver_refresh
    @validate_arguments
    def _recognize_loop(self, refresh_rate: float):
        try:
            self.start_record()
            result_field = self.driver.find_element(*recognized_text_field)
            last_value = ""
            last_activation_time = time.time()
            MAX_FIELD_LENGTH = 3500
            INACTIVITY_CLEAR_TIMEOUT = 10

            while not self._stop_event.is_set():
                current_value = self.driver.execute_script("return arguments[0].value;", result_field)
                
                # Очистка при переполнении
                if (len(current_value) > MAX_FIELD_LENGTH and 
                    time.time() - last_activation_time > INACTIVITY_CLEAR_TIMEOUT):
                    result_field.clear()
                    current_value = last_value = ""
                    continue

                if current_value != last_value:
                    last_value = current_value
                    
                    # Режим с требованием имени ассистента
                    if self.require_assistant_name:
                        if self.activation_required:
                            if recognized := self.__find_after_any_keyword(current_value, self.assistant_names):
                                self.recognized_text = recognized
                                self.activation_required = False
                                last_activation_time = time.time()
                                self.__reset_inactivity_timer()
                                result_field.clear()
                        elif current_value.strip():
                            self.recognized_text = current_value
                            last_activation_time = time.time()
                            self.__reset_inactivity_timer()
                            result_field.clear()
                    
                    # Режим без требования имени
                    elif current_value.strip():
                        self.recognized_text = current_value
                        result_field.clear()

                time.sleep(refresh_rate)
        except Exception as e:
            logging.error(f"Ошибка распознавания: {e}")
        finally:
            self.stop_record()

    def stop_recognize(self):
        if self._recognize_thread:
            self._stop_event.set()
            self._recognize_thread.join(timeout=1.0)
            self._recognize_thread = None

    def recognize(self, refresh_rate: float = 0.05) -> str:
        while True:
            if self.recognized_text:
                text = self.recognized_text
                self.recognized_text = None
                return text
            time.sleep(refresh_rate)

    def exit(self):
        logging.info("ЗАВЕРШЕНИЕ РАБОТЫ")
        self.stop_recognize()
        self.driver.quit()