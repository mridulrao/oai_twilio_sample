import os
import json
import base64
import asyncio
import websockets
import time
from fastapi import FastAPI, WebSocket, Request, Response, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect, Say, Stream, Dial, Conference
from twilio.rest import Client
from dotenv import load_dotenv
from typing import Dict, Set, List

load_dotenv()

# Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_NUMBER = os.getenv('TWILIO_NUMBER')
AI_AGENT_NUMBER = os.getenv('AI_AGENT_NUMBER')
PORT = int(os.getenv('PORT', '8000'))
RENDER_URL = os.getenv('RENDER_URL')

# Initialize Twilio client
client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Get moderator numbers from environment
MODERATORS: List[str] = os.getenv('MODERATOR_NUMBERS', '').split(',')
MODERATORS = [num.strip() for num in MODERATORS if num.strip()]

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


class ConferenceManager:
    def __init__(self):
        self._conferences: Dict[str, Dict] = {}
        self._lock = asyncio.Lock()
        self._participants: Dict[str, Set[str]] = {}  # Track participants per conference
    
    async def add_moderator_and_check_first(self, conference_name: str, moderator_number: str) -> bool:
        async with self._lock:
            # Check if the conference exists and has active flag
            conf = self._conferences.get(conference_name)
            if not conf or not conf.get('is_active', False):
                print(f"Conference {conference_name} doesn't exist or is not active, creating new")
                await self.reset_conference_state(conference_name)  # Reset state first
                conf = self._conferences[conference_name]
                is_first = True  # Always true for new or reset conference
            else:
                is_first = False
                
            conf['moderators'].add(moderator_number)
            self._participants[conference_name].add(moderator_number)
            conf['last_activity'] = time.time()
            
            print(f"\nConference state for {conference_name}:")
            print(f"Is first moderator: {is_first}")
            print(f"Current moderators: {conf['moderators']}")
            print(f"Current participants: {self._participants[conference_name]}")
            print(f"Conference active: {conf['is_active']}")
            print(f"Agent dialed: {conf['agent_dialed']}\n")
            
            return is_first
        
    async def reset_conference_state(self, conference_name: str) -> None:
        """Reset conference state but keep the conference in memory"""
        async with self._lock:
            if conference_name in self._conferences:
                print(f"\nResetting state for conference {conference_name}")
                self._conferences[conference_name] = {
                    'is_active': False,
                    'agent_dialed': False,
                    'moderators': set(),
                    'last_activity': time.time()
                }
                self._participants[conference_name] = set()
                print("Conference state has been reset to inactive\n")

    async def set_conference_active(self, conference_name: str, active: bool = True) -> None:
        async with self._lock:
            if conference_name not in self._conferences:
                self._conferences[conference_name] = {
                    'is_active': active,
                    'agent_dialed': False,
                    'moderators': set(),
                    'last_activity': time.time()
                }
            else:
                self._conferences[conference_name]['is_active'] = active
                self._conferences[conference_name]['last_activity'] = time.time()
            print(f"Set conference {conference_name} active status to: {active}")

    async def set_agent_dialed(self, conference_name: str, dialed: bool = True) -> None:
        async with self._lock:
            if conference_name not in self._conferences:
                self._conferences[conference_name] = {
                    'is_active': False,
                    'agent_dialed': dialed,
                    'moderators': set(),
                    'last_activity': time.time()
                }
            else:
                self._conferences[conference_name]['agent_dialed'] = dialed
                self._conferences[conference_name]['last_activity'] = time.time()
            print(f"Set conference {conference_name} agent dialed status to: {dialed}")

    async def add_participant(self, conference_name: str, participant_number: str) -> None:
        """Add a participant to the conference"""
        async with self._lock:
            if conference_name not in self._participants:
                self._participants[conference_name] = set()
            self._participants[conference_name].add(participant_number)
            print(f"Added participant {participant_number} to conference {conference_name}")
            print(f"Current participants: {self._participants[conference_name]}")

    async def remove_participant(self, conference_name: str, participant_number: str) -> None:
        """Remove a participant from the conference"""
        async with self._lock:
            if conference_name in self._participants:
                self._participants[conference_name].discard(participant_number)
                print(f"Removed participant {participant_number} from conference {conference_name}")
                print(f"Remaining participants: {self._participants[conference_name]}")
                
                # If no participants left, reset the conference
                if not self._participants[conference_name]:
                    await self.reset_conference(conference_name)
                    print(f"No participants left, conference {conference_name} has been reset")

    async def reset_conference_state(self, conference_name: str) -> None:
        """Reset conference state but keep the conference in memory"""
        async with self._lock:
            if conference_name in self._conferences:
                print(f"\nResetting state for conference {conference_name}")
                self._conferences[conference_name] = {
                    'is_active': False,
                    'agent_dialed': False,
                    'moderators': set(),
                    'last_activity': time.time()
                }
                if conference_name in self._participants:
                    self._participants[conference_name] = set()
                print("Conference state has been reset to inactive\n")

    async def cleanup_old_conferences(self, max_age_hours: int = 24) -> None:
        """Clean up old conference data"""
        current_time = time.time()
        async with self._lock:
            to_remove = []
            for conf_name, conf in self._conferences.items():
                if current_time - conf['last_activity'] > max_age_hours * 3600:
                    to_remove.append(conf_name)
            for conf_name in to_remove:
                await self.reset_conference(conf_name)
                print(f"Cleaned up old conference: {conf_name}")

    async def check_and_cleanup_stale_conference(self, conference_name: str) -> None:
        """Check if conference is stale and clean it up if needed"""
        async with self._lock:
            if conference_name in self._conferences:
                conf = self._conferences[conference_name]
                # If more than 5 minutes since last activity, reset the conference
                if time.time() - conf['last_activity'] > 300:  # 300 seconds = 5 minutes
                    print(f"Conference {conference_name} appears stale, resetting...")
                    await self.reset_conference_state(conference_name)
                    return True
            return False
        
    async def setup_conference_for_moderator(self, conference_name: str, moderator_number: str) -> bool:
        """Handle all conference setup operations atomically"""
        async with self._lock:
            try:
                # Check if conference exists and is active
                conf = self._conferences.get(conference_name)
                is_new = not conf or not conf.get('is_active', False)
                
                if is_new:
                    print(f"Setting up new conference: {conference_name}")
                    self._conferences[conference_name] = {
                        'is_active': True,  # Set active immediately
                        'agent_dialed': False,
                        'moderators': set([moderator_number]),
                        'last_activity': time.time()
                    }
                    self._participants[conference_name] = set([moderator_number])
                else:
                    print(f"Adding moderator to existing conference: {conference_name}")
                    conf['moderators'].add(moderator_number)
                    self._participants[conference_name].add(moderator_number)
                    conf['last_activity'] = time.time()
                
                print(f"\nConference state for {conference_name}:")
                print(f"Is new conference: {is_new}")
                print(f"Current moderators: {self._conferences[conference_name]['moderators']}")
                print(f"Current participants: {self._participants[conference_name]}")
                print(f"Conference active: {self._conferences[conference_name]['is_active']}")
                print(f"Agent dialed: {self._conferences[conference_name]['agent_dialed']}\n")
                
                return is_new
                
            except Exception as e:
                print(f"Error in setup_conference_for_moderator: {e}")
                raise

# Initialize FastAPI and Conference Manager
app = FastAPI()
conference_manager = ConferenceManager()

# Root endpoint
@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Unified Twilio Conference and Media Stream Server is running!"}

# Voice endpoint
@app.api_route("/voice", methods=["GET", "POST"])
async def call(request: Request, background_tasks: BackgroundTasks):
    query_params = dict(request.query_params)
    from_number = query_params.get('From')
    conference_name = 'My conference'
    
    print(f"\n=== New Call Request ===")
    print(f"Caller number: {from_number}")
    print(f"Expected moderators: {MODERATORS}")
    
    response = VoiceResponse()
    
    if from_number in MODERATORS:
        try:
            is_new = await conference_manager.setup_conference_for_moderator(conference_name, from_number)
            
            dial = Dial()
            callback_url = f"{RENDER_URL}/status"
            print(f"Setting status callback URL to: {callback_url}")
            
            # Modified conference configuration
            dial.conference(
                conference_name,
                start_conference_on_enter=True,
                end_conference_on_exit=True,
                status_callback=callback_url,
                status_callback_event="start end join leave completed",  # Changed to space-separated string
                status_callback_method="POST",
                recording_status_callback=callback_url,  # Add recording callback
                recording_status_callback_method="POST",
                record=True,  # Enable recording to ensure we get status updates
                endConferenceOnExit=True  # Explicit end on moderator exit
            )
            
            # If this is a new conference, initiate AI agent
            if is_new:
                print("New conference - initiating AI agent call")
                try:
                    await initiate_ai_agent_call()
                    async with conference_manager._lock:
                        conf = conference_manager._conferences[conference_name]
                        conf['agent_dialed'] = True
                    print("AI agent call initiated successfully")
                except Exception as e:
                    print(f"Error initiating AI agent call: {e}")
            
            response.append(dial)
            print("Added moderator dial instruction to response")
            
        except Exception as e:
            print(f"Error in conference setup: {e}")
            # Fallback - let moderator join anyway
            dial = Dial()
            dial.conference(
                conference_name,
                start_conference_on_enter=True,
                end_conference_on_exit=True
            )
            response.append(dial)
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
    
    print("=== Call Request Processed ===\n")
    return Response(content=str(response), media_type="application/xml")
# Wait endpoint
@app.api_route("/wait", methods=["GET", "POST"])
async def wait():
    response = VoiceResponse()
    response.say("Please wait while we connect you to the conference.")
    return Response(content=str(response), media_type="application/xml")

# Status callback endpoint
@app.api_route("/status", methods=["GET", "POST"])
async def status_callback(request: Request):
    print("\n=== Received Status Callback ===")
    try:
        form_data = await request.form()
        status_data = dict(form_data)
        print("Raw status data:", status_data)
        
        # Get all possible status indicators
        conference_status = status_data.get('ConferenceStatus')
        event_type = status_data.get('StatusCallbackEvent')
        event_name = status_data.get('EventName')
        call_status = status_data.get('CallStatus')
        recording_status = status_data.get('RecordingStatus')
        
        print(f"Conference Status: {conference_status}")
        print(f"Event Type: {event_type}")
        print(f"Event Name: {event_name}")
        print(f"Call Status: {call_status}")
        print(f"Recording Status: {recording_status}")
        
        # Check all possible end conditions
        should_reset = any([
            call_status == 'completed',
            conference_status == 'completed',
            event_type == 'conference-end',
            event_name == 'conference-end',
            recording_status == 'completed'
        ])
        
        if should_reset:
            print("Conference end detected, resetting state")
            conference_name = status_data.get('ConferenceName', 'My conference')
            await conference_manager.reset_conference_state(conference_name)
            print("Conference state reset completed")
        
        print("=== Status Callback Processed ===\n")
        return Response(content="OK", status_code=200)
    except Exception as e:
        print(f"Error in status callback: {str(e)}")
        return Response(content="Error", status_code=500)

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
    response.start().stream(url=f"{RENDER_URL}/stream")
    dial = Dial()
    dial.conference(
        'My conference',
        start_conference_on_enter=True,
        muted=False
    )
    response.append(dial)
    return Response(content=str(response), media_type="application/xml")

# WebSocket endpoint
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
    try:
        call = client.calls.create(
            url=f"{RENDER_URL}/ai-agent",
            to=AI_AGENT_NUMBER,
            from_=TWILIO_NUMBER
        )
        print(f"Successfully initiated call to AI agent: {call.sid}")
        return call
    except Exception as e:
        print(f"Error initiating AI agent call: {e}")
        raise

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)