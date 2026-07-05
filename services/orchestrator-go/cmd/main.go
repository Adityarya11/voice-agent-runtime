package main

import (
	"context"
	"flag"
	"fmt"
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

	// ── MILESTONE 4 EMPTY BUFFER GUARD TEST ──────────────────────────────────
	// Sends a rogue END_OF_UTTERANCE before any audio has been buffered.
	// Expected: Python logs a warning and continues. No crash, no state corruption.
	// Remove this block before production use.
	logWarn("[M4-TEST] Sending rogue END_OF_UTTERANCE with empty buffer...")
	if err := currentSession.StreamUtterance(""); err == nil {
		// StreamUtterance with empty path will fail at os.Open — handle below instead
	}

	// Send the control signal directly since there is no audio to stream
	err = stream.Send(&pb.Event{
		SessionId: currentSession.ID,
		Payload: &pb.Event_Control{
			Control: &pb.ControlSignal{
				Type: pb.ControlSignal_END_OF_UTTERANCE,
			},
		},
	})
	if err != nil {
		logWarn("[M4-TEST] Rogue signal send error: %v", err)
	}
	logInfo("[M4-TEST] Rogue END_OF_UTTERANCE sent. Waiting for Python to acknowledge...")
	// ── END MILESTONE 4 TEST ──────────────────────────────────────────────────

	logInfo("Streaming utterance 1...")
	if err := currentSession.StreamUtterance("../../test_data/input_1.wav"); err != nil {
		logFatal("Utterance 1 failed: %v", err)
	}
	logInfo("Utterance 1 complete.")

	logInfo("Streaming utterance 2...")
	if err := currentSession.StreamUtterance("../../test_data/input_2.wav"); err != nil {
		logFatal("Utterance 2 failed: %v", err)
	}
	logInfo("Utterance 2 complete.")

	<-currentSession.DoneChan
	logInfo("Both utterances processed. Session complete.")
}
