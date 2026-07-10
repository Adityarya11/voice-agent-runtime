package session

import (
	"fmt"
	"io"
	"log"
	"os"
	"sync"
	"time"

	pb "voice-runtime/orchestrator-go/generated"
	"voice-runtime/orchestrator-go/internal/config"
)

type SessionState string

const (
	StateCreated    SessionState = "CREATED"
	StateConnecting SessionState = "CONNECTING"
	StateActive     SessionState = "ACTIVE"
	StateProcessing SessionState = "PROCESSING"
	StateResponding SessionState = "RESPONDING"
	StateTerminated SessionState = "TERMINATED"
)

var validTransitions = map[SessionState][]SessionState{
	StateCreated:    {StateConnecting},
	StateConnecting: {StateActive},
	StateActive:     {StateProcessing, StateResponding, StateTerminated},
	StateProcessing: {StateResponding, StateTerminated},
	StateResponding: {StateActive, StateTerminated},
	StateTerminated: {},
}

type Session struct {
	ID        string
	Profile   *config.AgentProfile
	State     SessionState
	StartedAt time.Time

	AgentAudioChan chan []byte
	InterruptChan  chan struct{}
	DoneChan       chan struct{}

	mu        sync.Mutex
	closeOnce sync.Once
	stream    pb.VoiceAgent_StreamEventsClient
}

func NewSession(id string, profile *config.AgentProfile) *Session {
	return &Session{
		ID:        id,
		Profile:   profile,
		State:     StateCreated,
		StartedAt: time.Now(),

		AgentAudioChan: make(chan []byte, 100),
		InterruptChan:  make(chan struct{}, 1),
		DoneChan:       make(chan struct{}),
	}
}

func (s *Session) transitionTo(next SessionState) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	allowed, ok := validTransitions[s.State]
	if !ok {
		return fmt.Errorf("session %s: unknown current state '%s'", s.ID, s.State)
	}

	for _, valid := range allowed {
		if valid == next {
			log.Printf("[Session %s] %s -> %s", s.ID, s.State, next)
			s.State = next
			return nil
		}
	}

	return fmt.Errorf(
		"session %s: illegal transition '%s' -> '%s'",
		s.ID, s.State, next,
	)
}

func (s *Session) GetState() SessionState {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.State
}

func (s *Session) Attach(stream pb.VoiceAgent_StreamEventsClient) error {
	if err := s.transitionTo(StateConnecting); err != nil {
		return err
	}
	s.stream = stream
	if err := s.transitionTo(StateActive); err != nil {
		return err
	}
	log.Printf("[Session %s] Stream attached. Agent: %s", s.ID, s.Profile.Name)
	return nil
}

func (s *Session) signalDone() {
	s.closeOnce.Do(func() {
		close(s.DoneChan)
		log.Printf("[Session %s] DoneChan closed.", s.ID)
	})
}

func (s *Session) sendAudioChunk(data []byte) error {
	return s.stream.Send(&pb.Event{
		SessionId: s.ID,
		Payload: &pb.Event_Audio{
			Audio: &pb.AudioChunk{Data: data},
		},
	})
}

func (s *Session) StreamAudio(audioPath string) error {
	f, err := os.Open(audioPath)
	if err != nil {
		return fmt.Errorf("session %s: failed to open audio file: %v", s.ID, err)
	}

	defer f.Close()

	buf := make([]byte, 4096)
	for {
		n, readErr := f.Read(buf)
		if n > 0 {
			chunk := make([]byte, n)
			copy(chunk, buf[:n])
			if sendErr := s.sendAudioChunk(chunk); sendErr != nil {
				return fmt.Errorf("session %s: audio send error: %v", s.ID, sendErr)
			}
		}
		if readErr == io.EOF {
			break
		}
		if readErr != nil {
			return fmt.Errorf("session %s: file read error: %v", s.ID, readErr)
		}
	}
	log.Printf("[Session %s] Audio streamed from '%s' (no boundary signal).", s.ID, audioPath)
	return nil
}

func (s *Session) StreamUtterance(audioPath string) error {
	if err := s.StreamAudio(audioPath); err != nil {
		return err
	}

	sendErr := s.stream.Send(&pb.Event{
		SessionId: s.ID,
		Payload: &pb.Event_Control{
			Control: &pb.ControlSignal{Type: pb.ControlSignal_END_OF_UTTERANCE},
		},
	})
	if sendErr != nil {
		return fmt.Errorf("session %s: END_OF_UTTERANCE send error: %v", s.ID, sendErr)
	}
	log.Printf("[Session %s] Utterance streamed from '%s'. END_OF_UTTERANCE sent.", s.ID, audioPath)
	return nil
}

func (s *Session) StreamSilence(durationMs int) error {
	const sampleRate = 44100
	const bytesPerSample = 2
	totalBytes := (sampleRate * bytesPerSample * durationMs) / 1000
	silence := make([]byte, totalBytes)

	chunkSize := 4096
	for offset := 0; offset < len(silence); offset += chunkSize {
		end := offset + chunkSize
		if end > len(silence) {
			end = len(silence)
		}
		if err := s.sendAudioChunk(silence[offset:end]); err != nil {
			return fmt.Errorf("session %s: silence send error: %v", s.ID, err)
		}
	}
	log.Printf("[Session %s] Streamed %dms of silence (%d bytes).", s.ID, durationMs, totalBytes)
	return nil
}

func (s *Session) Run() {
	s.readPump()
}

func (s *Session) readPump() {
	defer close(s.AgentAudioChan)
	defer s.signalDone()

	firstChunk := true

	for {
		event, err := s.stream.Recv()
		if err == io.EOF {
			log.Printf("[Session %s] Stream closed by inference engine.", s.ID)
			if err := s.transitionTo(StateTerminated); err != nil {
				log.Printf("[Session %s] readPump terminal transition error: %v", s.ID, err)
			}
			return
		}
		if err != nil {
			log.Printf("[Session %s] readPump recv error: %v", s.ID, err)
			return
		}

		if audio := event.GetAudio(); audio != nil {
			if firstChunk {
				if err := s.transitionTo(StateResponding); err != nil {
					log.Printf("[Session %s] readPump transition error: %v", s.ID, err)
				}
				firstChunk = false
			}
			s.AgentAudioChan <- audio.Data
		}
	}
}

func (s *Session) Terminate() {
	if err := s.transitionTo(StateTerminated); err != nil {
		log.Printf("[Session %s] Terminate: %v", s.ID, err)
		return
	}
	s.signalDone()
	log.Printf("[Session %s] Terminated.", s.ID)
}
