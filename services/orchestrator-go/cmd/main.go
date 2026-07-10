package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"time"

	pb "voice-runtime/orchestrator-go/generated"
	"voice-runtime/orchestrator-go/internal/config"
	"voice-runtime/orchestrator-go/internal/session"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

const timeFormat = "2006-01-02 15:04:05"
const moduleName = "Orchestrator"

func logInfo(format string, v ...any) {
	msg := fmt.Sprintf(format, v...)
	fmt.Printf("[%s] [INFO] [%s] %s\n", time.Now().Format(timeFormat), moduleName, msg)
}

func logWarn(format string, v ...any) {
	msg := fmt.Sprintf(format, v...)
	fmt.Printf("[%s] [WARNING] [%s] %s\n", time.Now().Format(timeFormat), moduleName, msg)
}

func logFatal(format string, v ...any) {
	msg := fmt.Sprintf(format, v...)
	fmt.Printf("[%s] [FATAL] [%s] %s\n", time.Now().Format(timeFormat), moduleName, msg)
	os.Exit(1)
}

func main() {
	profileName := flag.String("profile", "receptionist", "Agent profile YAML name")
	flag.Parse()

	logInfo("Booting Voice Runtime — duplex milestone 3 test.")

	agentConfig, err := config.LoadProfile(*profileName)
	if err != nil {
		logFatal("Failed to load profile: %v", err)
	}
	logInfo("Profile loaded: %s", agentConfig.Name)

	conn, err := grpc.NewClient(
		"localhost:50051",
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		logFatal("Failed to connect to inference engine: %v", err)
	}
	defer conn.Close()

	client := pb.NewVoiceAgentClient(conn)
	stream, err := client.StreamEvents(context.Background())
	if err != nil {
		logFatal("Failed to open stream: %v", err)
	}

	currentSession := session.NewSession("session_prod_001", agentConfig)

	if err := currentSession.Attach(stream); err != nil {
		logFatal("Failed to attach stream to session: %v", err)
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
		logFatal("Failed to send START_SESSION: %v", err)
	}

	outputFile, err := os.Create("../../test_data/output_m3.raw")
	if err != nil {
		logFatal("Failed to create output file: %v", err)
	}
	defer outputFile.Close()

	go func() {
		for chunk := range currentSession.AgentAudioChan {
			outputFile.Write(chunk)
		}
	}()

	go currentSession.Run()

	// M4-TEST: manual END_OF_UTTERANCE override, verified in true-duplex
	// milestone 4 (empty-buffer guard). Left disabled since VAD milestone 4
	// proved boundaries fire correctly without it -- see docs/vad.md.
	// logWarn("[M4-TEST] Sending rogue END_OF_UTTERANCE with empty buffer...")
	// err = stream.Send(&pb.Event{
	// 	SessionId: currentSession.ID,
	// 	Payload: &pb.Event_Control{
	// 		Control: &pb.ControlSignal{
	// 			Type: pb.ControlSignal_END_OF_UTTERANCE,
	// 		},
	// 	},
	// })
	// if err != nil {
	// 	logWarn("[M4-TEST] Rogue signal send error: %v", err)
	// }

	log.Println("[Orchestrator] Streaming utterance 1 (VAD-only boundary detection)...")
	if err := currentSession.StreamAudio("../../test_data/input_1.wav"); err != nil {
		log.Fatalf("[Orchestrator] Utterance 1 audio failed: %v", err)
	}

	log.Println("[Orchestrator] Injecting silence gap...")
	if err := currentSession.StreamSilence(700); err != nil {
		log.Fatalf("[Orchestrator] Silence injection failed: %v", err)
	}

	log.Println("[Orchestrator] Streaming utterance 2 (VAD-only boundary detection)...")
	if err := currentSession.StreamAudio("../../test_data/input_2.wav"); err != nil {
		log.Fatalf("[Orchestrator] Utterance 2 audio failed: %v", err)
	}

	log.Println("[Orchestrator] Injecting trailing silence to close utterance 2...")
	if err := currentSession.StreamSilence(700); err != nil {
		log.Fatalf("[Orchestrator] Trailing silence failed: %v", err)
	}

	<-currentSession.DoneChan
	logInfo("VAD autonomy test complete. Session closed.")
}
