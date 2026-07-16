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

func sendStartSession(stream pb.VoiceAgent_StreamEventsClient, sess *session.Session) error {
	return stream.Send(&pb.Event{
		SessionId: sess.ID,
		Payload: &pb.Event_Control{
			Control: &pb.ControlSignal{
				Type: pb.ControlSignal_START_SESSION,
				Profile: &pb.AgentProfile{
					AgentName:    sess.Profile.Name,
					SystemPrompt: sess.Profile.SystemPrompt,
				},
			},
		},
	})
}

func startOutputCapture(agentAudioChan <-chan []byte, path string) (*os.File, error) {
	f, err := os.Create(path)
	if err != nil {
		return nil, fmt.Errorf("failed to create output file: %w", err)
	}
	go func() {
		for chunk := range agentAudioChan {
			f.Write(chunk)
		}
	}()
	return f, nil
}

func runTestSequence(sess *session.Session) error {
	logInfo("Streaming utterance 1 (VAD-only boundary detection)...")
	if err := sess.StreamAudio("../../test_data/input_1.wav"); err != nil {
		return fmt.Errorf("utterance 1 audio failed: %w", err)
	}

	logInfo("Injecting silence gap...")
	if err := sess.StreamSilence(700); err != nil {
		return fmt.Errorf("silence injection failed: %w", err)
	}

	logInfo("Streaming utterance 2 (VAD-only boundary detection)...")
	if err := sess.StreamAudio("../../test_data/input_2.wav"); err != nil {
		return fmt.Errorf("utterance 2 audio failed: %w", err)
	}

	logInfo("Injecting trailing silence to close utterance 2...")
	if err := sess.StreamSilence(700); err != nil {
		return fmt.Errorf("trailing silence failed: %w", err)
	}

	return nil
}

func main() {
	profileName := flag.String("profile", "receptionist", "Agent profile YAML name")
	flag.Parse()

	logInfo("Booting Voice Runtime.")

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

	if err := sendStartSession(stream, currentSession); err != nil {
		logFatal("Failed to send START_SESSION: %v", err)
	}

	outputFile, err := startOutputCapture(currentSession.AgentAudioChan, "../../test_data/output_m3.raw")
	if err != nil {
		logFatal("%v", err)
	}
	defer outputFile.Close()

	go currentSession.Run()

	if err := runTestSequence(currentSession); err != nil {
		logFatal("Test sequence failed: %v", err)
	}

	<-currentSession.DoneChan
	logInfo("VAD autonomy test complete. Session closed.")
}
