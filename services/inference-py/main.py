import grpc 
from concurrent import futures
import time 
import sys
import os 

sys.path.append(os.path.join(os.path.dirname(__file__), 'grpc_server'))

import agent_pb2
import agent_pb2_grpc

class VoiceAgentServicer(agent_pb2_grpc.VoiceAgentServicer):
    def StreamEvents(self, request_iteratior, context):
        print("[Python] Stream Connected")

        for event in request_iteratior: 
            session_id = event.session_id

            if event.HasField('audio'):
                print(f"[Python] Received audio chunk for session {session_id} ({len(event.audio.data)} bytes)")
                
                # Simulate AI processing time
                time.sleep(0.1)

                yield agent_pb2.Event(
                    session_id = session_id,
                    audio=agent_pb2.AudioChunk(data=b"dummy_ai_audio_vytes")
                )
            
            elif event.HasField('control'):
                print(f"[Python] Recieved the control signal: {event.control.type}")

def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    agent_pb2_grpc.add_VoiceAgentServicer_to_server(VoiceAgentServicer(), server)
    server.add_insecure_port('[::]:50051')
    print("[Python] Inference Engine starting on port 50051...")
    server.start()
    server.wait_for_termination()

if __name__ == "__main__":
    serve()