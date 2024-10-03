import argparse
import nfc
import os
import pytz
import socket
import time
from azure.cognitiveservices.speech import SpeechConfig, SpeechRecognizer
from azure.cognitiveservices.speech.audio import PushAudioInputStream, AudioConfig
from threading import Event, Thread
from threading import Event
from datetime import datetime
from dotenv import load_dotenv
from openai import OpenAI
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
from robottools import RobotTools
from state_machine_stream import StateMachineThread
from card_reader import CardReaderThread

class ReceiveAudioUDPAndPushStreamThread:
    """ 音声を受信しストリームに流すためのクラス """
    def __init__(self, stop_event: Event, asr_start_event: Event):
        self._ip = "0.0.0.0"
        self._port = 5001
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((self._ip, self._port))
        self._asr_start_event = Event()
        self._thread = None
        self._asr_start_event = asr_start_event
        self._stop_event = stop_event

    def _run(self):
        while not self._stop_event.is_set():
            data, addr = self._sock.recvfrom(4096)
            if self._asr_start_event.is_set():
                push_stream.write(data)

    def start(self):
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()

# コマンドライン引数
parse = argparse.ArgumentParser()
parse.add_argument("--ip", required=True)
parse.add_argument("--port", default=22222, type=int)
args = parse.parse_args()

# .env設定ファイル
load_dotenv()

# Azure Speech Recognition
speech_config = SpeechConfig(subscription=os.environ.get("AZURE_API_KEY"), region=os.environ.get("AZURE_SERVICE_REGION"))
speech_config.speech_recognition_language=os.environ.get("AZURE_SPEECH_RECOGNITION_LANGUAGE")
push_stream = PushAudioInputStream()
audio_config = AudioConfig(stream=push_stream)

with (OpenAI(api_key=os.environ.get("OPENAI_API_KEY")) as openai_client,
      MongoClient(os.environ.get("MONGO_HOST"), server_api=ServerApi('1')) as mongo_client,
      nfc.ContactlessFrontend(os.environ.get("NFC_PATH")) as nfc_client):
    
    azure_asr_client = SpeechRecognizer(speech_config=speech_config, audio_config=audio_config)
    # RobotController
    robotcontroller_client = RobotTools(args.ip, args.port)
    # Client objects
    clients = dict(
        OPENAI_CLIENT=openai_client,
        MONGO_CLIENT=mongo_client,
        AZURE_ASR_CLIENT=azure_asr_client,
        ROBOTCONTROLLER_CLIENT=robotcontroller_client,
        NFC_CLIENT=nfc_client
    )
    # StopEventObject for threads
    STOP_EVENT = Event()
    # ASRStartEventObject for ReceiveAudioUDPAndPushStreamThread
    ASR_START_EVENT = Event()
    # CardReaderThread
    crt = CardReaderThread(clients, STOP_EVENT)
    crt.start()
    # StateMachineThread
    smt = StateMachineThread(clients, STOP_EVENT, ASR_START_EVENT)
    smt.start()
    # ReceiveAudioUDPAndPushStreamThread
    audt = ReceiveAudioUDPAndPushStreamThread(STOP_EVENT, ASR_START_EVENT)
    audt.start()
    # Event loop
    try:
        while True:
            text = input("")
            if text == "quit":
                break
    except KeyboardInterrupt:
        pass

    STOP_EVENT.set()

