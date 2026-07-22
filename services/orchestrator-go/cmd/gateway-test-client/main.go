package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"log"
	"os"

	gatewaypb "voice-runtime/orchestrator-go/generated/gateway"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

func main() {
	gatewayAddr := flag.String("gateway", "localhost:50052", "Gateway server address")
	audioPath := flag.String("audio", "../../test_data/input.wav", "Input WAV file to stream")
	outputPath := flag.String("output", "../../test_data/gateway_test_output.raw", "Output raw PCM file")
	sampleRate := flag.Int("rate", 44100, "Source sample rate to declare in START_SESSION")
	sessionID := flag.String("session", "gateway_test_001", "Session ID")
	flag.Parse()

	conn, err := grpc.NewClient(*gatewayAddr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatalf("Failed to connect to gateway: %v", err)
	}
	defer conn.Close()

	client := gatewaypb.NewGatewayClient(conn)
	stream, err := client.StreamAudio(context.Background())
	if err != nil {
		log.Fatalf("Failed to open StreamAudio: %v", err)
	}

	err = stream.Send(&gatewaypb.GatewayEvent{
		SessionId: *sessionID,
		Payload: &gatewaypb.GatewayEvent_Control{
			Control: &gatewaypb.GatewayControl{
				Type:             gatewaypb.GatewayControl_START_SESSION,
				SourceSampleRate: int32(*sampleRate),
			},
		},
	})
	if err != nil {
		log.Fatalf("Failed to send START_SESSION: %v", err)
	}
	log.Printf("Sent START_SESSION (session_id=%s, source_sample_rate=%d)", *sessionID, *sampleRate)

	outFile, err := os.Create(*outputPath)
	if err != nil {
		log.Fatalf("Failed to create output file: %v", err)
	}
	defer outFile.Close()

	recvDone := make(chan struct{})
	go func() {
		defer close(recvDone)
		totalBytes := 0
		for {
			event, err := stream.Recv()
			if err == io.EOF {
				log.Println("Server closed stream (EOF).")
				return
			}
			if err != nil {
				log.Printf("Recv error: %v", err)
				return
			}
			if audio := event.GetAudio(); audio != nil {
				n, werr := outFile.Write(audio.Data)
				if werr != nil {
					log.Printf("Write error: %v", werr)
					return
				}
				totalBytes += n
				log.Printf("Received %d bytes (total: %d)", n, totalBytes)
			}
		}
	}()

	if err := streamAudioFile(stream, *audioPath, *sessionID); err != nil {
		log.Fatalf("Failed to stream audio file: %v", err)
	}
	log.Println("Finished streaming audio file.")

	err = stream.Send(&gatewaypb.GatewayEvent{
		SessionId: *sessionID,
		Payload: &gatewaypb.GatewayEvent_Control{
			Control: &gatewaypb.GatewayControl{
				Type: gatewaypb.GatewayControl_END_SESSION,
			},
		},
	})
	if err != nil {
		log.Printf("Failed to send END_SESSION: %v", err)
	} else {
		log.Println("Sent END_SESSION.")
	}

	<-recvDone
	log.Println("Test client complete.")
}

func streamAudioFile(stream gatewaypb.Gateway_StreamAudioClient, path string, sessionID string) error {
	f, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("failed to open audio file: %v", err)
	}
	defer f.Close()

	buf := make([]byte, 4096)
	for {
		n, readErr := f.Read(buf)
		if n > 0 {
			chunk := make([]byte, n)
			copy(chunk, buf[:n])
			sendErr := stream.Send(&gatewaypb.GatewayEvent{
				SessionId: sessionID,
				Payload: &gatewaypb.GatewayEvent_Audio{
					Audio: &gatewaypb.AudioChunk{Data: chunk},
				},
			})
			if sendErr != nil {
				return fmt.Errorf("audio send error: %v", sendErr)
			}
		}
		if readErr == io.EOF {
			break
		}
		if readErr != nil {
			return fmt.Errorf("file read error: %v", readErr)
		}
	}
	return nil
}
