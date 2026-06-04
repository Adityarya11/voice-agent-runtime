package main

import (
	"context"
	"fmt"
	"io"
	"log"
	"os"

	pb "voice-runtime/orchestrator-go/generated"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

func main() {
	fmt.Println("[Go] Starting Orchestrator...")

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

	sessionID := "session_001"
	fmt.Printf("[Go] Connected to Python for %s\n", sessionID)

	// 1. Prepare to receive the AI's audio response
	outputFile, err := os.Create("../../test_data/output.raw")
	if err != nil {
		log.Fatalf("Failed to create output file: %v", err)
	}
	defer outputFile.Close()

	waitc := make(chan struct{})
	go func() {
		bytesReceived := 0
		for {
			in, err := stream.Recv()
			if err == io.EOF {
				close(waitc)
				return
			}
			if err != nil {
				log.Fatalf("Failed to receive from Python: %v", err)
			}
			if in.GetAudio() != nil {
				data := in.GetAudio().Data
				outputFile.Write(data)
				bytesReceived += len(data)
				fmt.Printf("\r[Go] Receiving AI Audio... %d bytes captured", bytesReceived)
			}
		}
	}()

	// 2. Read local input.wav and stream it to Python
	inputFile, err := os.Open("../../test_data/input.wav")
	if err != nil {
		log.Fatalf("Failed to open input.wav: %v", err)
	}
	defer inputFile.Close()

	buffer := make([]byte, 4096) // 4KB chunks
	for {
		n, err := inputFile.Read(buffer)
		if err == io.EOF {
			break
		}
		if err != nil {
			log.Fatalf("Error reading input file: %v", err)
		}

		stream.Send(&pb.Event{
			SessionId: sessionID,
			Payload: &pb.Event_Audio{
				Audio: &pb.AudioChunk{Data: buffer[:n]},
			},
		})
	}

	fmt.Println("[Go] Finished streaming user audio. Waiting for AI...")

	// CloseSend tells Python the stream is done, triggering inference
	stream.CloseSend()

	// Wait for Python to finish sending the AI audio back
	<-waitc
	fmt.Println("\n[Go] Stream complete. AI audio saved to test_data/output.raw")
}
