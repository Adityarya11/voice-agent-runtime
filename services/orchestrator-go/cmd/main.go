package main

import (
	"context"
	"fmt"
	"io"
	"log"
	"time"

	pb "voice-runtime/orchestrator-go/generated"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

func main() {
	fmt.Println("[Go] Starting Orchestrator...")

	// Connect to Python server
	conn, err := grpc.NewClient("localhost:50051", grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatalf("Did not connect: %v", err)
	}
	defer conn.Close()

	client := pb.NewVoiceAgentClient(conn)
	stream, err := client.StreamEvents(context.Background())
	if err != nil {
		log.Fatalf("Error creating stream: %v", err)
	}

	sessionID := "session_win_001"
	fmt.Printf("[Go] Established stream for %s\n", sessionID)

	// Send Control Signal
	stream.Send(&pb.Event{
		SessionId: sessionID,
		Payload: &pb.Event_Control{
			Control: &pb.ControlSignal{Type: pb.ControlSignal_START_SESSION},
		},
	})

	// Goroutine to receive from Python
	waitc := make(chan struct{})
	go func() {
		for {
			in, err := stream.Recv()
			if err == io.EOF {
				close(waitc)
				return
			}
			if err != nil {
				log.Fatalf("Failed to receive a note : %v", err)
			}
			if in.GetAudio() != nil {
				fmt.Printf("[Go] Received AI audio chunk: %s\n", string(in.GetAudio().Data))
			}
		}
	}()

	// Simulate streaming 5 audio chunks from a user
	for i := 1; i <= 5; i++ {
		dummyData := []byte(fmt.Sprintf("user_audio_chunk_%d", i))
		fmt.Printf("[Go] Sending audio chunk %d...\n", i)
		stream.Send(&pb.Event{
			SessionId: sessionID,
			Payload: &pb.Event_Audio{
				Audio: &pb.AudioChunk{Data: dummyData},
			},
		})
		time.Sleep(500 * time.Millisecond) // Wait half a second between chunks
	}

	stream.CloseSend()
	<-waitc
	fmt.Println("[Go] Stream finished. Orchestrator shutting down.")
}
