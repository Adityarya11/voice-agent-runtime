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

	Profile       *config.AgentProfile
	InferenceAddr string
}

func NewServer(profile *config.AgentProfile, inferenceAddr string) *Server {
	return &Server{
		Profile:       profile,
		InferenceAddr: inferenceAddr,
	}
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

	conn, err := grpc.NewClient(
		s.InferenceAddr,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		return fmt.Errorf("gateway: failed to connect to inference engine: %v", err)
	}
	defer conn.Close()

	agentClient := agentpb.NewVoiceAgentClient(conn)
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

	for {
		_, err := stream.Recv()
		if err == io.EOF {
			log.Printf("[Gateway] Session %s: AetherRTC closed stream.", sessionID)
			return nil
		}
		if err != nil {
			return fmt.Errorf("gateway: session %s recv error: %v", sessionID, err)
		}
	}

}
