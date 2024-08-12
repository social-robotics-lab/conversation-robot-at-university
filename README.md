# conversation-robot-at-university
A robot system that talks to students at a university.

## 動作環境
- Windows
- Python3.12
- MongoDB Atlas

## 事前準備
- カードリーダー（RC-S380/S）を利用するための環境設定
  - [card-reader-for-RC-S380](https://github.com/social-robotics-lab/card-reader-for-RC-S380)の事前準備を参照
- MongoDB Atlas
  - カードリーダーのイベントを保持するコレクションと会話データを保持するコレクションを作成
  - 会話内容の埋め込みベクトルを検索するためのVectorSearchIndexを作成（以下は例）
```
{
  "fields": [
    {
      "numDimensions": 1536,
      "path": "conversation_data_embedding",
      "similarity": "cosine",
      "type": "vector"
    },
    {
      "path": "user_id",
      "type": "filter"
    },
    {
      "path": "conversation_id",
      "type": "filter"
    }
  ]
}
```
  - MongoDB Atlasではアクセス元のIPアドレスでアクセス制限している。実行前にMongoDB AtlasのWebサイトにアクセスし、IPアドレスを登録しておく。

## 仮想環境の作成
- `python -m venv myenv`

## モジュールのインストール
- `pip install -r requirements.txt`

## .envファイルの編集
.envファイルを作成し、以下の内容を入力
```
OPENAI_API_KEY="OpenAIのAPIキー"
OPENAI_API_MODEL="gpt-4o"
OPENAI_API_EMBEDDING_MODEL="text-embedding-3-small"
AZURE_API_KEY="Microsoft AzureのSpeechServicesのAPIキー"
AZURE_SERVICE_REGION="japanwest"
AZURE_SPEECH_RECOGNITION_LANGUAGE="ja-JP"
MONGO_HOST="MongoDB Atlasにアクセスするためのコード mongodb+srv://アカウント名…の文字列"
MONGO_DB_NAME="conversation_db"
MONGO_COLLECTION_CONVERSATION_DATA="conversation_data"
MONGO_COLLECTION_CARD_READER_EVENTS="card_reader_events"
MONGO_DB_ATLAS_USER="MongoDB Atlasのアカウント名"
MONGO_DB_ATLAS_PASS="MongoDB Atlasのパスワード"
NFC_PATH="usb"
LOCATION="任意の場所の名称　例：counseling_room"
ROBOT_ID="000"
ROBOT_NAME="ソータ"
```

## プログラムの実行
- `myenv\Script\activate`
- `python app.py`

実行後、カードリーダーに学生証をかざすと会話が開始される。「さよなら」のような会話の終了を示唆する発話をすると、会話は終了する。会話の終了後にユーザーの発話の要約と

## 注意事項
- 音声認識はPC上のマイクを使って行われる。

---
## プログラムの内容
state_machine.pyに書かれている状態遷移に従って動作する。
