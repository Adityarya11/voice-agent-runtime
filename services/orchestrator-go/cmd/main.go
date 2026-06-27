package main

import (
	"context"
	"flag"
	"io"
	"log"
	"os"

	pb "voice-runtime/orchestrator-go/generated"
	"voice-runtime/orchestrator-go/internal/config"
	"voice-runtime/orchestrator-go/internal/session"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

func main() {
	profileName := flag.String("profile", "receptionist", "Agent profile YAML name")
	flag.Parse()

	log.Println("[Orchestrator] Booting Voice Runtime...")

	agentConfig, err := config.LoadProfile(*profileName)
	if err != nil {
		log.Fatalf("[Orchestrator] Failed to load profile: %v", err)
	}
	log.Printf("[Orchestrator] Profile loaded: %s", agentConfig.Name)

	conn, err := grpc.NewClient(
		"localhost:50051",
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		log.Fatalf("[Orchestrator] Failed to connect to inference engine: %v", err)
	}
	defer conn.Close()

	client := pb.NewVoiceAgentClient(conn)
	stream, err := client.StreamEvents(context.Background())
	if err != nil {
		log.Fatalf("[Orchestrator] Failed to open stream: %v", err)
	}

	currentSession := session.NewSession("session_prod_001", agentConfig)

	if err := currentSession.Attach(stream); err != nil {
		log.Fatalf("[Orchestrator] Failed to attach stream to session: %v", err)
	}

	err = stream.Send(&pb.Event{
		SessionId: currentSession.ID,
		Payload: &pb.Event_Control{
			Control: &pb.ControlSignal{
				Type: pb.ControlSignal_START_SESSION,
				Profile: &pb.AgentProfile{
					AgentName:    currentSession.Profile.Name,
					SystemPrompt: currentSession.Profile.SystemPrompt,
				},
			},
		},
	})
	if err != nil {
		log.Fatalf("[Orchestrator] Failed to send control signal: %v", err)
	}

	go currentSession.Run()

	inputFile, err := os.Open("../../test_data/input.wav")
	if err != nil {
		log.Fatalf("[Orchestrator] Failed to open input file: %v", err)
	}
	defer inputFile.Close()

	buffer := make([]byte, 4096)
	for {
		n, err := inputFile.Read(buffer)
		if n > 0 {
			chunk := make([]byte, n)
			copy(chunk, buffer[:n])
			currentSession.UserAudioChan <- chunk
		}
		if err == io.EOF {
			break
		}
		if err != nil {
			log.Printf("[Orchestrator] File read error: %v", err)
			break
		}
	}

	close(currentSession.UserAudioChan)
	log.Println("[Orchestrator] Audio feed complete. END_OF_UTTERANCE will be dispatched. Stream remains open.")

	outputFile, err := os.Create("../../test_data/output.raw")
	if err != nil {
		log.Fatalf("[Orchestrator] Failed to create output file: %v", err)
	}
	defer outputFile.Close()

	for chunk := range currentSession.AgentAudioChan {
		outputFile.Write(chunk)
	}

	<-currentSession.DoneChan
	log.Println("[Orchestrator] Session completed via duplex path. Stream closed by inference engine.")
}
