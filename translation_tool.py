import os
import json
import base64
import asyncio
import websockets
from fastapi import FastAPI, WebSocket, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect, Say, Stream, Dial, Conference
from twilio.rest import Client
from dotenv import load_dotenv

load_dotenv()

# Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_NUMBER = os.getenv('TWILIO_NUMBER')
MODERATOR = os.getenv('MODERATOR_NUMBER')
AI_AGENT_NUMBER = os.getenv('AI_AGENT_NUMBER')
PORT = int(os.getenv('PORT', '8000'))

# Initialize Twilio client
client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
RENDER_URL = "https://oai-twilio-sample.onrender.com"

# OpenAI Configuration
VOICE = 'alloy'
LOG_EVENT_TYPES = [
    'response.content.done', 'rate_limits.updated', 'response.done',
    'input_audio_buffer.committed', 'input_audio_buffer.speech_stopped',
    'input_audio_buffer.speech_started', 'session.created'
]

SYSTEM_MESSAGE = '''
    You are a bilingual translation assistant proficient in English and Spanish. Your task is to translate all user inputs from one language to the other. 

    Guidelines:
    - **Introduction**: When the interaction begins, introduce yourself by saying, "Hello, I am your bilingual translation assistant. I will help translate between English and Spanish."
    - **Translation**:
        - If the user speaks in **English**, translate their input into **Spanish**.
        - If the user speaks in **Spanish**, translate their input into **English**.
    - **Response Format**: Provide only the translated text in the target language without adding any additional comments or personal opinions.
    - **Handling Mixed Languages**: If the user's input contains both English and Spanish, translate each part into the opposite language accordingly.
    - **Non-Translatable Input**: If the input is not in English or Spanish, respond with, "Can you say that again, I didnt understood."
    - **Tone and Style**: Use clear and professional language appropriate for general audiences.
    - **Errors and Clarifications**: If you're unsure about a translation, respond with your best attempt and politely indicate any uncertainty.
'''

# Create unified FastAPI app
app = FastAPI()

if not OPENAI_API_KEY:
    raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')

# Root endpoint
@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Unified Twilio Conference and Media Stream Server is running!"}

# Conference call endpoints
@app.api_route("/voice", methods=["GET", "POST"])
async def call(request: Request):
    query_params = dict(request.query_params)
    from_number = query_params.get('From')
    
    print(f"Caller number: {from_number}")
    print(f"Expected moderator: {MODERATOR}")
    
    response = VoiceResponse()
    
    if from_number == MODERATOR:
        print("Moderator joined")
        dial = Dial()
        dial.conference(
            'My conference',
            start_conference_on_enter=True,
            end_conference_on_exit=True,
            status_callback=f"https://{request.base_url.hostname}/status"
        )
        response.append(dial)
        
        print("Dialing Agent")
        await initiate_ai_agent_call() 
        print("Agent Connected")     
    else:
        print(f"Regular participant joined")
        dial = Dial()
        dial.conference(
            'My conference',
            start_conference_on_enter=False,
            wait_url=f"https://{request.base_url.hostname}/wait",
            wait_method="GET"
        )
        response.append(dial)
    
    return Response(content=str(response), media_type="application/xml")

@app.api_route("/wait", methods=["GET", "POST"])
async def wait():
    response = VoiceResponse()
    response.say("Please wait while we connect you to the conference.")
    return Response(content=str(response), media_type="application/xml")

@app.api_route("/status", methods=["POST"])
async def status_callback(request: Request):
    form_data = await request.form()
    status_data = dict(form_data)
    conference_status = status_data.get('ConferenceStatus')
    
    print(f"Conference Status Update: {status_data}")
    
    if conference_status == 'completed':
        print("Conference ended")
    
    return Response(content="OK")

# AI Agent endpoints
@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    response = VoiceResponse()
    response.say("Please wait while we connect your call to agent")
    response.pause(length=1)
    response.say("O.K. you can start talking!")
    host = request.url.hostname
    connect = Connect()
    connect.stream(url=f'wss://{host}/media-stream')
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.api_route("/ai-agent", methods=["GET", "POST"])
async def ai_agent(request: Request):
    response = VoiceResponse()
    response.start().stream(url=f"{RENDER_URL}/stream") #conference ngrok
    dial = Dial()
    dial.conference(
        'My conference',
        start_conference_on_enter=True,
        muted=False
    )
    response.append(dial)
    return Response(content=str(response), media_type="application/xml")

@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    print("Client connected")
    await websocket.accept()
    async with websockets.connect(
        'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01',
        extra_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1"
        }
    ) as openai_ws:
        await send_session_update(openai_ws)
        stream_sid = None
        
        async def receive_from_twilio():
            nonlocal stream_sid
            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    if data['event'] == 'media' and openai_ws.open:
                        audio_append = {
                            "type": "input_audio_buffer.append",
                            "audio": data['media']['payload']
                        }
                        await openai_ws.send(json.dumps(audio_append))
                    elif data['event'] == 'start':
                        stream_sid = data['start']['streamSid']
                        print(f"Incoming stream has started {stream_sid}")
            except WebSocketDisconnect:
                print("Client disconnected.")
                if openai_ws.open:
                    await openai_ws.close()

        async def send_to_twilio():
            nonlocal stream_sid
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)
                    if response['type'] in LOG_EVENT_TYPES:
                        print(f"Received event: {response['type']}", response)
                    if response['type'] == 'session.updated':
                        print("Session updated successfully:", response)
                    if response['type'] == 'response.audio.delta' and response.get('delta'):
                        try:
                            audio_payload = base64.b64encode(base64.b64decode(response['delta'])).decode('utf-8')
                            audio_delta = {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {
                                    "payload": audio_payload
                                }
                            }
                            await websocket.send_json(audio_delta)
                        except Exception as e:
                            print(f"Error processing audio data: {e}")
            except Exception as e:
                print(f"Error in send_to_twilio: {e}")

        await asyncio.gather(receive_from_twilio(), send_to_twilio())

# Helper functions
async def send_session_update(openai_ws):
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad"},
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": VOICE,
            "instructions": SYSTEM_MESSAGE,
            "modalities": ["text", "audio"],
            "temperature": 0.8,
        }
    }
    print('Sending session update:', json.dumps(session_update))
    await openai_ws.send(json.dumps(session_update))

async def initiate_ai_agent_call():
    call = client.calls.create(
        url=f"{RENDER_URL}/ai-agent", # ai ngrok
        to=AI_AGENT_NUMBER,
        from_=TWILIO_NUMBER
    )
    print(f"Initiated call to AI agent: {call.sid}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)