package gateway

import (
	"fmt"
	"io"
	"log"
	agentpb "voice-runtime/orchestrator-go/generated"
	gatewaypb "voice-runtime/orchestrator-go/generated/gateway"
	"voice-runtime/orchestrator-go/internal/config"
	"voice-runtime/orchestrator-go/internal/session"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

type Server struct {
	gatewaypb.UnimplementedGatewayServer

	Profile *config.AgentProfile
	conn    *grpc.ClientConn
}

func NewServer(profile *config.AgentProfile, inferenceAddr string) (*Server, error) {

	conn, err := grpc.NewClient(
		inferenceAddr,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		return nil, fmt.Errorf("gateway: failed to connect to inference engine: %v", err)
	}

	return &Server{
		Profile: profile,
		conn:    conn,
	}, nil
}

func (s *Server) StreamAudio(stream gatewaypb.Gateway_StreamAudioServer) error {
	first, err := stream.Recv()
	if err != nil {
		return fmt.Errorf("gateway: failed to recieve initial event: %v", err)
	}

	control := first.GetControl()
	if control == nil || control.Type != gatewaypb.GatewayControl_START_SESSION {
		return fmt.Errorf("gateway: expected START_SESSION as first event; got %T", first.Payload)
	}

	sessionID := first.SessionId
	sourceSampleRate := control.SourceSampleRate

	log.Printf("[Gateway] Incoming session %s, source_sample_rate=%d", sessionID, sourceSampleRate)

	agentClient := agentpb.NewVoiceAgentClient(s.conn)
	agentStream, err := agentClient.StreamEvents(stream.Context())
	if err != nil {
		return fmt.Errorf("gateway: failed to open agent stream: %v", err)
	}

	sess := session.NewSession(sessionID, s.Profile)
	if err := sess.Attach(agentStream); err != nil {
		return fmt.Errorf("gateway: failed to attach the session: %v", err)
	}

	err = agentStream.Send(&agentpb.Event{
		SessionId: sessionID,
		Payload: &agentpb.Event_Control{
			Control: &agentpb.ControlSignal{
				Type:             agentpb.ControlSignal_START_SESSION,
				SourceSampleRate: sourceSampleRate,
				Profile: &agentpb.AgentProfile{
					AgentName:    sess.Profile.Name,
					SystemPrompt: sess.Profile.SystemPrompt,
				},
			},
		},
	})

	if err != nil {
		return fmt.Errorf("gateway: failed to send START_SESSION to inference engine: %v", err)
	}

	log.Printf("[Gateway] Session %s attached to inference engine.", sessionID)

	go sess.Run()

	outboundDone := make(chan struct{})
	go func() {
		defer close(outboundDone)
		for chunk := range sess.AgentAudioChan {
			err := stream.Send(&gatewaypb.GatewayEvent{
				SessionId: sessionID,
				Payload: &gatewaypb.GatewayEvent_Audio{
					Audio: &gatewaypb.AudioChunk{
						Data: chunk,
					},
				},
			})

			if err != nil {
				log.Printf("[Gateway] session %s: outbound send to AetherRTC failed: %v", sessionID, err)
				return
			}
		}
	}()

	inboundErr := make(chan error, 1)
	go func() {
		for {
			event, err := stream.Recv()
			if err != nil {
				inboundErr <- err
				return
			}
			if audio := event.GetAudio(); audio != nil {
				if err := sess.SendAudio(audio.Data); err != nil {
					inboundErr <- err
					return
				}
				continue
			}

			if control := event.GetControl(); control != nil && control.Type == gatewaypb.GatewayControl_END_SESSION {
				inboundErr <- io.EOF
				return
			}
		}
	}()

	select {
	case <-sess.DoneChan:
		log.Printf("[Gateway] session %s: Python side ended.", sessionID)
	case err := <-inboundErr:
		if err == io.EOF {
			log.Printf("[Gateway] session %s: AetherRTC closed stream.", sessionID)
		} else {
			log.Printf("[Gateway] session %s: AetherRTC recv error: %v", sessionID, err)
		}
	}
	<-outboundDone
	return nil

}
