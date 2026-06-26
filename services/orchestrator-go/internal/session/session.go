package session

import (
	"fmt"
	"io"
	"log"
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

	UserAudioChan  chan []byte
	AgentAudioChan chan []byte
	InterruptChan  chan struct{}
	DoneChan       chan struct{}

	mu     sync.Mutex
	stream pb.VoiceAgent_StreamEventsClient
}

func NewSession(id string, profile *config.AgentProfile) *Session {
	return &Session{
		ID:        id,
		Profile:   profile,
		State:     StateCreated,
		StartedAt: time.Now(),

		UserAudioChan:  make(chan []byte, 100),
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

func (s *Session) Run() {
	var wg sync.WaitGroup
	wg.Add(2)

	go func() {
		defer wg.Done()
		s.writePump()
	}()

	go func() {
		defer wg.Done()
		s.readPump()
	}()

	wg.Wait()
}

func (s *Session) writePump() {
	for chunk := range s.UserAudioChan {
		err := s.stream.Send(&pb.Event{
			SessionId: s.ID,
			Payload: &pb.Event_Audio{
				Audio: &pb.AudioChunk{Data: chunk},
			},
		})
		if err != nil {
			log.Printf("[Session %s] writePump send error: %v", s.ID, err)
			return
		}
	}

	err := s.stream.Send(&pb.Event{
		SessionId: s.ID,
		Payload: &pb.Event_Control{
			Control: &pb.ControlSignal{
				Type: pb.ControlSignal_END_OF_UTTERANCE,
			},
		},
	})
	if err != nil {
		log.Printf("[Session %s] writePump failed to send END_OF_UTTERANCE: %v", s.ID, err)
		return
	}

	log.Printf("[Session %s] END_OF_UTTERANCE sent. Stream remains open.", s.ID)
}

func (s *Session) readPump() {
	defer close(s.AgentAudioChan)
	defer close(s.DoneChan)

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
	log.Printf("[Session %s] Terminated.", s.ID)
}
