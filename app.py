import argparse
import nfc
import os
import pytz
from azure.cognitiveservices.speech import SpeechConfig, SpeechRecognizer
from threading import Event
from datetime import datetime
from dotenv import load_dotenv
from openai import OpenAI
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
from robottools import RobotTools
from state_machine import StateMachineThread
from card_reader import CardReaderThread


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

with (OpenAI(api_key=os.environ.get("OPENAI_API_KEY")) as openai_client,
      MongoClient(os.environ.get("MONGO_HOST"), server_api=ServerApi('1')) as mongo_client,
      nfc.ContactlessFrontend(os.environ.get("NFC_PATH")) as nfc_client):
    
    azure_asr_client = SpeechRecognizer(speech_config=speech_config)
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
    # CardReaderThread
    crt = CardReaderThread(clients, STOP_EVENT)
    crt.start()
    # StateMachineThread
    smt = StateMachineThread(clients, STOP_EVENT)
    smt.start()
    # Event loop
    try:
        while True:
            text = input("")
            if text == "quit":
                break
    except KeyboardInterrupt:
        pass

    STOP_EVENT.set()

