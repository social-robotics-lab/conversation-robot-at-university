import json
import os
import prompt_template
import pytz
import time
from azure.cognitiveservices.speech import ResultReason, CancellationReason
from bson import json_util
from datetime import datetime, timezone, timedelta
from openai import OpenAI
from pymongo import DESCENDING
from robottools import RobotTools
from threading import Event, Thread


class StateMachineThread:
    """ 状態遷移を管理する状態遷移マシン """
    def __init__(self, clients: dict, stop_event: Event):
        self._clients = clients
        self._thread = None
        self._stop_event = stop_event
        self._current_state = None

    def _run(self):
        self._current_state = Init(self._clients, self._stop_event)
        while not self._stop_event.is_set():
            next_state = self._current_state.run()
            self._current_state = next_state

    def start(self):
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()
        

class State:
    """ 状態の親クラス """
    def __init__(self, clients: dict, stop_event: Event):
        self._clients = clients
        self._stop_event = stop_event
        self._jst = pytz.timezone('Asia/Tokyo')
        self._state_start_time = datetime.now(self._jst)
        self._db = clients["MONGO_CLIENT"][os.environ.get("MONGO_DB_NAME")]
        self._card_reader_events_collection = self._db[os.environ.get("MONGO_COLLECTION_CARD_READER_EVENTS")]
        self._conversation_data_collection = self._db[os.environ.get("MONGO_COLLECTION_CONVERSATION_DATA")]
        print(f"Creating State: {self.__class__.__name__}")

    def run(self):
        pass


class Init(State):
    """ 設定を初期化する状態 """
    def __init__(self, clients: dict, stop_event: Event):
        super().__init__(clients, stop_event)
        
    def run(self):
        print("起動しました。")
        say_with_beat_motion(self._clients["ROBOTCONTROLLER_CLIENT"], text="起動しました。")
        return Wait(self._clients, self._stop_event)


class Wait(State):
    """ 待機状態 """
    def __init__(self, clients: dict, stop_event: Event):
        super().__init__(clients, stop_event)
        become_wait_mode(self._clients["ROBOTCONTROLLER_CLIENT"])

    def run(self) -> State:
        while not self._stop_event.is_set():
            # card_reader_events_collectionをポーリング
            # Waitステートに入ってから新しいdocumentがあるかを確認
            latest_document = self._card_reader_events_collection.find_one({"location": os.environ.get("LOCATION"), "start_time": {"$gte": self._state_start_time}}, sort=[("start_time", DESCENDING)])
            if latest_document:
                conversation_start_time = datetime.now(self._jst)
                conversation_id = f"{latest_document['user_id']}_{conversation_start_time.strftime('%Y%m%d%H%M%S')}"
                doc = self._conversation_data_collection.find_one({"user_id": latest_document['user_id']}, {"conversation_data_embedding": 0}, sort=[("conversation_start_time", DESCENDING)])
                last_conversation_data = doc if doc else {}
                conversation_data = {
                    "conversation_id": conversation_id,
                    "conversation_start_time": conversation_start_time,
                    "conversation_end_time": None,
                    "user_id": latest_document['user_id'],
                    "user_last_name": latest_document['user_last_name'],
                    "robot_id": latest_document['robot_id'],
                    "robot_name": latest_document['robot_name'],
                    "location": latest_document['location'],
                    "conversation_contents": [],
                    "user_info": last_conversation_data["user_info"] if last_conversation_data else {},
                    "conversation_data_embedding": []
                }
                self._conversation_data_collection.insert_one(conversation_data)
                return Greet(self._clients, self._stop_event, conversation_id)
            time.sleep(0.5)


class Greet(State):
    """ 挨拶状態 """
    def __init__(self, clients: dict, stop_event: Event, conversation_id: str):
        super().__init__(clients, stop_event)
        become_talk_mode(self._clients["ROBOTCONTROLLER_CLIENT"])
        self._conversation_id = conversation_id
        self._current_conversation_data = self._conversation_data_collection.find_one({"conversation_id": conversation_id})
        docs = self._conversation_data_collection.find({"user_id": self._current_conversation_data["user_id"]}, {"conversation_data_embedding": 0}).sort("conversation_start_time", -1).skip(1).limit(1)
        self._last_conversation_data = {}
        for doc in docs:
            self._last_conversation_data = doc
            break

    def run(self) -> State:
        prompt = make_greet_prompt(self._current_conversation_data, self._last_conversation_data)
        clauses = []
        for clause in openai_clause_gen(self._clients["OPENAI_CLIENT"], prompt):
            print("ROBOT: " + clause)
            say_with_beat_motion(self._clients["ROBOTCONTROLLER_CLIENT"], text=clause)
            clauses.append(clause)

        robot_message = "".join(clauses)
        conversation_content = dict(timestamp=self._state_start_time, speaker="assistant", message=robot_message)
        self._conversation_data_collection.update_one(
            {"conversation_id": self._conversation_id},
            {"$push": {"conversation_contents": conversation_content}}
        )
        return SpeechRecognition(self._clients, self._stop_event, self._conversation_id)


class SpeechRecognition(State):
    """ 音声認識状態 """
    def __init__(self, clients: dict, stop_event: Event, conversation_id: str):
        super().__init__(clients, stop_event)
        self._conversation_id = conversation_id
        self._current_conversation_data = self._conversation_data_collection.find_one({"conversation_id": conversation_id})
        self._recognized_time = self._state_start_time

    def run(self) -> State:
        while not self._stop_event.is_set():
            result = self._clients["AZURE_ASR_CLIENT"].recognize_once()
            if result.reason == ResultReason.RecognizedSpeech:
                user_message = result.text
                print(f"user: {user_message}")
                play_nod_motion(self._clients["ROBOTCONTROLLER_CLIENT"])
                conversation_content = dict(start_time=datetime.now(self._jst), speaker="user", message=user_message)
                self._conversation_data_collection.update_one(
                    {"conversation_id": self._conversation_id},
                    {"$push": {"conversation_contents": conversation_content}}
                )
                # 発話をまだ続ける意図があると判断されたとき、音声認識を繰り返す。そうでなければ、Responseステートへ遷移。                
                if openai_determine_if_speaker_intend_to_continue_to_utterance(self._clients["OPENAI_CLIENT"], self._current_conversation_data, user_message):
                    self._recognized_time = datetime.now(self._jst)
                    continue
                else:
                    return Response(self._clients, self._stop_event, self._conversation_id)
            elif result.reason == ResultReason.NoMatch:
                print("No speech could be recognized")
            elif result.reason == ResultReason.Canceled:
                cancellation_details = result.cancellation_details
                print(f"Speech Recognition canceled: {cancellation_details.reason}")
                if cancellation_details.reason == CancellationReason.Error:
                    print(f"Error details: {cancellation_details.error_details}")
            # 認識が30秒以上行われなかったとき、EstimtePersonalityAndStoreEmbeddingステートに遷移
            if datetime.now(self._jst) - self._recognized_time > timedelta(seconds=30):
                return EstimtePersonalityAndStoreEmbedding(self._clients, self._stop_event, self._conversation_id)



class Response(State):
    """ 応答状態 """
    def __init__(self, clients: dict, stop_event: Event, conversation_id: str):
        super().__init__(clients, stop_event)
        self._conversation_id = conversation_id
        self._current_conversation_data = self._conversation_data_collection.find_one({"conversation_id": self._conversation_id})
        docs = self._conversation_data_collection.find({"user_id": self._current_conversation_data["user_id"]}, {"conversation_data_embedding": 0}).sort("conversation_start_time", -1).skip(1).limit(1)
        self._last_conversation_data = {}
        for doc in docs:
            self._last_conversation_data = doc
            break
        embedding = openai_create_embedding(self._clients["OPENAI_CLIENT"], json.dumps(self._current_conversation_data, default=json_util.default))
        self._related_conversation_data = self.find_similar_conversations(embedding)

    def run(self) -> State:
        prompt = make_response_prompt(self._current_conversation_data, self._last_conversation_data, self._related_conversation_data)
        clauses = []
        for clause in openai_clause_gen(self._clients["OPENAI_CLIENT"], prompt):
            print("ROBOT: " + clause)
            say_with_beat_motion(self._clients["ROBOTCONTROLLER_CLIENT"], text=clause)
            clauses.append(clause)
        
        robot_message = "".join(clauses)
        conversation_content = dict(timestamp=datetime.now(self._jst), speaker="assistant", message=robot_message)
        self._conversation_data_collection.update_one(
            {"conversation_id": self._conversation_id},
            {"$push": {"conversation_contents": conversation_content}}
        )
        # 話者が会話を終了しようとしている意図があると判断されたとき、Waitステートへ遷移。そうでなければ、SpeechRecognitionステートへ遷移。
        if openai_determine_if_speaker_intend_to_close_dialogue(self._clients["OPENAI_CLIENT"], self._current_conversation_data["conversation_contents"][-1]["message"]):
            return EstimtePersonalityAndStoreEmbedding(self._clients, self._stop_event, self._conversation_id)
        return SpeechRecognition(self._clients, self._stop_event, self._conversation_id)


    def find_similar_conversations(self, query_embedding:list , top_n=5):
        pipeline = [
            {
                "$vectorSearch": 
                    {
                        "index": "conversation_data_vector_index", 
                        "path": "conversation_data_embedding", 
                        "queryVector": query_embedding,
                        "numCandidates": 150, 
                        "limit": top_n,
                        "filter": {"user_id": self._current_conversation_data["user_id"], "conversation_id": {"$ne": self._conversation_id}}
                    }
            },
            {
                "$project":
                    {
                        "conversation_id": 1,
                        "conversation_start_time": 1,
                        "user_id": 1,
                        "user_last_name": 1,
                        "robot_id": 1,
                        "robot_name": 1,
                        "location": 1,
                        "conversation_contents": 1,
                        "user_info": 1,
                        "score": {"$meta": "vectorSearchScore"}
                    }
            }
        ]

        docs = self._conversation_data_collection.aggregate(pipeline)
        for doc in docs:
            return doc
        return {}

class EstimtePersonalityAndStoreEmbedding(State):
    """ 会話データの埋め込みベクトルを生成・保存する状態 """
    def __init__(self, clients: dict, stop_event: Event, conversation_id: str):
        super().__init__(clients, stop_event)
        self._conversation_id = conversation_id
        self._current_conversation_data = self._conversation_data_collection.find_one({"conversation_id": self._conversation_id})
        docs = self._conversation_data_collection.find({"user_id": self._current_conversation_data["user_id"]}, {"conversation_data_embedding": 0}).sort("conversation_start_time", -1).skip(1).limit(1)
        self._last_conversation_data = {}
        for doc in docs:
            self._last_conversation_data = doc
            break
        
    def run(self):
        user_info = openai_extract_user_info(self._clients["OPENAI_CLIENT"], self._current_conversation_data, self._last_conversation_data)
        current_conversation_data_str = json.dumps(self._current_conversation_data, default=json_util.default)
        current_conversation_data_embedding = openai_create_embedding(self._clients["OPENAI_CLIENT"], current_conversation_data_str)
        self._conversation_data_collection.update_one(
            {"conversation_id": self._conversation_id},
            {"$set": {"user_info": user_info, "conversation_data_embedding": current_conversation_data_embedding}}
        )
        return Wait(self._clients, self._stop_event)


#--------------
# OpenAITools
#--------------
def openai_clause_gen(openai_client: OpenAI, prompt: list):
    """ 生成された文字列を句読点ごとに返すジェネレータ """
    stream = openai_client.chat.completions.create(
        model=os.environ.get("OPENAI_API_MODEL"),
        messages=prompt,
        stream=True
    )
    chunks = []
    for chunk in stream:
        token = chunk.choices[0].delta.content
        if token is None:
            if len(chunks) != 0:
                clause = "".join(chunks)
                chunks.clear()
                if clause:
                    yield clause
        else:
            token = token.replace("「", "").replace("」", "").replace("*", "")
            # トークン内の句読点をチェック
            for punct in "。、；：！？":
                if punct in token:
                    parts = token.split(punct)
                    for i, part in enumerate(parts):
                        chunks.append(part)
                        if i < len(parts) - 1:
                            chunks.append(punct)
                            clause = "".join(chunks)
                            chunks.clear()
                            yield clause
                    break
            else:
                chunks.append(token)

def openai_create_embedding(openai_client: OpenAI, text: str) -> list:
    response = openai_client.embeddings.create(
        input=text,
        model=os.environ.get("OPENAI_API_EMBEDDING_MODEL"),
    )
    embedding = response.data[0].embedding
    return embedding        


def openai_determine_if_speaker_intend_to_continue_to_utterance(openai_client: OpenAI, current_conversation_data: dict, user_message: str) -> bool:
    system_prompt_str = prompt_template.determine_if_speaker_intend_to_continue_to_utterance.format(
        user_message=user_message
    )
    completion = openai_client.chat.completions.create(
        model=os.environ.get("OPENAI_API_MODEL"),
        messages=[ {"role": "system", "content": system_prompt_str} ]
    )
    judge = completion.choices[0].message.content
    return "待機" in judge

def openai_determine_if_speaker_intend_to_close_dialogue(openai_client: OpenAI, user_message: str) -> bool:
    system_prompt_str = prompt_template.determine_if_speaker_intend_to_close_dialogue.format(
        user_message=user_message
    )
    completion = openai_client.chat.completions.create(
        model=os.environ.get("OPENAI_API_MODEL"),
        messages=[ {"role": "system", "content": system_prompt_str} ]
    )
    return "終了" in completion.choices[0].message.content

def openai_extract_user_info(openai_client: OpenAI, current_conversation_data: dict, last_conversation_data: dict) -> str:
    system_prompt_str = prompt_template.extract_user_info.format(
        last_user_info=last_conversation_data["user_info"] if last_conversation_data else {},
        current_conversaiton_info="\n".join(["{speaker}: {message}".format(speaker=content['speaker'], message=content['message']) for content in current_conversation_data["conversation_contents"]])
    )
    prompt = [ {"role": "system", "content": system_prompt_str} ]
    completion = openai_client.chat.completions.create(
        model=os.environ.get("OPENAI_API_MODEL"),
        response_format={ "type": "json_object" },
        messages=prompt
    )
    text = completion.choices[0].message.content
    try:
        json_output = json.loads(text)
        return json_output
    except (TypeError, ValueError) as e:
        # JSON形式で出力できていなければ、前回のuser_infoをそのまま使う
        return current_conversation_data["user_info"]

#--------------
# Prompt
#--------------
def make_greet_prompt(current_conversation_data: dict, last_conversation_data: dict) -> list:
    jst = pytz.timezone('Asia/Tokyo')
    system_prompt_str = prompt_template.greeting_system_prompt.format(
        robot_settings=prompt_template.robot_settings,
        user_last_name=current_conversation_data["user_last_name"],
        user_info=current_conversation_data["user_info"],
        last_conversation_info=prompt_template.previous_conversation_info.format(
            previous_conversation_start_time=last_conversation_data["conversation_start_time"].replace(tzinfo=pytz.utc).astimezone(jst).strftime("%Y年%m月%d日%H時%M分%S秒"),
            previous_conversation_location=last_conversation_data["location"],
            previous_conversation_contents="\n".join(["  {speaker}: {message}".format(speaker=content['speaker'], message=content['message']) for content in last_conversation_data["conversation_contents"]])
        ) if last_conversation_data else "なし",
        current_datetime_jst=current_conversation_data["conversation_start_time"].replace(tzinfo=pytz.utc).astimezone(jst).strftime("%Y年%m月%d日%H時%M分%S秒"),
        suggestions=prompt_template.greeting_suggestions
    )

    return [{"role": "system", "content": system_prompt_str}]



def make_response_prompt(current_conversation_data: dict, last_conversation_data: dict, related_conversation_data: dict) -> list:
    jst = pytz.timezone('Asia/Tokyo')
    system_prompt_str = prompt_template.response_system_prompt.format(
        robot_settings=prompt_template.robot_settings,
        user_last_name=current_conversation_data["user_last_name"],
        user_info=current_conversation_data["user_info"],
        last_conversation_info=prompt_template.previous_conversation_info.format(
            previous_conversation_start_time=last_conversation_data["conversation_start_time"].replace(tzinfo=pytz.utc).astimezone(jst).strftime("%Y年%m月%d日%H時%M分%S秒"),
            previous_conversation_location=last_conversation_data["location"],
            previous_conversation_contents="\n".join(["  {speaker}: {message}".format(speaker=content['speaker'], message=content['message']) for content in last_conversation_data["conversation_contents"]])
        ) if last_conversation_data else "なし",
        related_conversation_info=prompt_template.previous_conversation_info.format(
            previous_conversation_start_time=related_conversation_data["conversation_start_time"].replace(tzinfo=pytz.utc).astimezone(jst).strftime("%Y年%m月%d日%H時%M分%S秒"),
            previous_conversation_location=related_conversation_data["location"],
            previous_conversation_contents="\n".join(["  {speaker}: {message}".format(speaker=content['speaker'], message=content['message']) for content in related_conversation_data["conversation_contents"]])
        ) if related_conversation_data else "なし",
        current_datetime_jst=current_conversation_data["conversation_start_time"].replace(tzinfo=pytz.utc).astimezone(jst).strftime("%Y年%m月%d日%H時%M分%S秒"),
        suggestions=prompt_template.response_suggestions
    )

    return [{"role": "system", "content": system_prompt_str}] + [{"role": e['speaker'], "content": e['message']} for e in current_conversation_data["conversation_contents"]]



#--------------
# RobotTools
#--------------
def play_nod_motion(robotcontroller: RobotTools):
    nod_motion = [
        dict(Msec=250, ServoMap=dict(HEAD_P=-15,)),
        dict(Msec=250, ServoMap=dict(HEAD_P=10, )),
        dict(Msec=250, ServoMap=dict(HEAD_P=-15, ))
    ]
    robotcontroller.play_motion(nod_motion)
    pass

def say_with_beat_motion(robotcontroller: RobotTools, text: str, speed=1.5):
    d = robotcontroller.say_text(text)
    m = robotcontroller.make_beat_motion(d, speed=speed)
    robotcontroller.play_motion(m)
    # 発話中ブロッキングする
    time.sleep(d)
    pass

def become_wait_mode(robotcontroller: RobotTools):
    servo_map = dict(HEAD_P=20)
    led_map = dict(R_EYE_R=10, R_EYE_G=10, R_EYE_B=10,
                L_EYE_R=10, L_EYE_G=10, L_EYE_B=10)
    pose = dict(Msec=500, ServoMap=servo_map, LedMap=led_map)
    robotcontroller.play_pose(pose)
    pass

def become_talk_mode(robotcontroller: RobotTools):
    servo_map = dict(HEAD_P=-15)
    led_map = dict(R_EYE_R=255, R_EYE_G=255, R_EYE_B=255,
                L_EYE_R=255, L_EYE_G=255, L_EYE_B=255)
    pose = dict(Msec=500, ServoMap=servo_map, LedMap=led_map)
    robotcontroller.play_pose(pose)
    pass