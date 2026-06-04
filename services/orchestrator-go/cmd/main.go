package main

import (
	"context"
	"flag"
	"fmt"
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

	profileName := flag.String("profile", "receptionist", "Name of the agent profile YAML to load")
	flag.Parse()

	fmt.Println("[Orchestrator] Booting Voice Runtime...")

	agentConfig, err := config.LoadProfile(*profileName)
	if err != nil {
		log.Fatalf("Critical: Could not load agent profile: %v", err)
	}
	fmt.Printf("[Orchestrator] Loaded Profile: %s\n", agentConfig.Name)

	currentSession := session.NewSession("session_prod_001", agentConfig)
	currentSession.Start()
	defer currentSession.Terminate()

	conn, err := grpc.NewClient("localhost:50051", grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatalf("Did not connect to Inference Engine: %v", err)
	}
	defer conn.Close()

	client := pb.NewVoiceAgentClient(conn)
	stream, err := client.StreamEvents(context.Background())
	if err != nil {
		log.Fatalf("Error creating stream: %v", err)
	}

	stream.Send(&pb.Event{
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

	outputFile, err := os.Create("../../test_data/output.raw")
	if err != nil {
		log.Fatalf("Failed to create output file: %v", err)
	}
	defer outputFile.Close()

	go func() {
		for {
			in, err := stream.Recv()
			if err == io.EOF {
				currentSession.DoneChan <- struct{}{}
				return
			}
			if err != nil {
				log.Printf("Stream error: %v", err)
				return
			}
			if in.GetAudio() != nil {
				outputFile.Write(in.GetAudio().Data)
			}
		}
	}()

	inputFile, err := os.Open("../../test_data/input.wav")
	if err != nil {
		log.Fatalf("Failed to open input.wav: %v", err)
	}
	defer inputFile.Close()

	buffer := make([]byte, 4096)
	for {
		n, err := inputFile.Read(buffer)
		if err == io.EOF {
			break
		}
		stream.Send(&pb.Event{
			SessionId: currentSession.ID,
			Payload: &pb.Event_Audio{
				Audio: &pb.AudioChunk{Data: buffer[:n]},
			},
		})
	}

	fmt.Println("[Orchestrator] User audio dispatched. Awaiting inference...")
	stream.CloseSend()
	<-currentSession.DoneChan
	fmt.Println("[Orchestrator] Call completed successfully.")
}
