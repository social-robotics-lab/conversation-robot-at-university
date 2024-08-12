import nfc
import os
import pytz
import time
import typing
import unicodedata
from datetime import datetime, timedelta
from pymongo import MongoClient
from threading import Event, Thread

class CardReaderThread:
    def __init__(self, clients: dict, stop_event: Event, connecting_time=3):
        self._clients = clients
        self._connecting_time = connecting_time
        self._thread = None
        self._stop_event = stop_event
        self._jst = pytz.timezone('Asia/Tokyo')
        db = clients["MONGO_CLIENT"][os.environ.get("MONGO_DB_NAME")]
        self._collection = db[os.environ.get("MONGO_COLLECTION_CARD_READER_EVENTS")]

    def _on_connect(self, tag: nfc.tag.Tag) -> bool:
        sys_code = 0xFE00
        service_code = 0x1A8B
        idm, pmm = tag.polling(system_code=sys_code)
        tag.idm, tag.pmm, tag.sys = idm, pmm, sys_code
        sc = nfc.tag.tt3.ServiceCode(service_code >> 6, service_code & 0x3F)
        # user ID
        bc = nfc.tag.tt3.BlockCode(0, service=0)
        user_id_block_bytearray = typing.cast(bytearray, tag.read_without_encryption([sc], [bc]))
        # user_id_block_unicode = user_id_block_bytearray.decode("shift_jis")
        # user_id = user_id_block_unicode[:10]
        user_id_bytearray = user_id_block_bytearray.split(b'\x00', 1)[0]
        user_id = user_id_bytearray.decode("shift-jis")
        print(f"user ID: {user_id}")
        # user Name
        bc = nfc.tag.tt3.BlockCode(1, service=0)
        user_name_block_bytearray = typing.cast(bytearray, tag.read_without_encryption([sc], [bc]))
        user_name_bytearray = user_name_block_bytearray.split(b'\x00', 1)[0]
        user_name = user_name_bytearray.decode("shift-jis")
        user_name = unicodedata.normalize('NFKC', user_name)
        print(f"user Name: {user_name}")
        current_time_jst = datetime.now(self._jst)
        card_reader_event = {
            "start_time": current_time_jst,
            "user_id": user_id,
            "user_last_name": user_name.split(" ")[0],
            "robot_id": os.environ.get("ROBOT_ID"),
            "robot_name": os.environ.get("ROBOT_NAME"),
            "location": os.environ.get("LOCATION"),
        }
        result = self._collection.insert_one(card_reader_event)
        print(f"Conversation started with id: {result.inserted_id}")
        return True

    def _on_release(self, tag: nfc.tag.Tag) -> None:
        print("released")

    def _run(self):
        try:
            while not self._stop_event.is_set():
                started = time.time()
                self._clients["NFC_CLIENT"].connect(rdwr={"on-connect": self._on_connect, 
                                                          "on-release": self._on_release},
                                                    terminate=lambda: time.time() - started > self._connecting_time)
        except Exception as e:
            print(f"Card reader is closed. {e}")

    def start(self):
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()
    with (MongoClient(os.environ.get("MONGODB_HOST")) as mongo_client,
          nfc.ContactlessFrontend(os.environ.get("NFC_PATH")) as nfc_client):
        
        clients = dict(
            MONGO_CLIENT=mongo_client,
            NFC_CLIENT=nfc_client
        )
        STOP_EVENT = Event()
        crt = CardReaderThread(clients, STOP_EVENT)
        crt.start()
        try:
            while True:
                text = input("> ")
                if text == "quit":
                    break
        except KeyboardInterrupt:
            pass
        STOP_EVENT.set()