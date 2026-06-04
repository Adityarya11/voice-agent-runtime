package session

import (
	"fmt"
	"time"
	"voice-runtime/orchestrator-go/internal/config"
)

type SessionState string

const (
	StateCreated    SessionState = "CREATED"
	StateConnecting SessionState = "CONNECTING"
	StateActive     SessionState = "ACTIVE"
	StateTerminated SessionState = "TERMINATED"
)

type Session struct {
	ID        string
	Profile   *config.AgentProfile
	State     SessionState
	StartedAt time.Time

	// channels
	UserAudioChan  chan []byte
	AgentAudioChan chan []byte
	DoneChan       chan struct{}
}

func NewSession(id string, profile *config.AgentProfile) *Session {
	return &Session{
		ID:        id,
		Profile:   profile,
		State:     StateCreated,
		StartedAt: time.Now(),

		UserAudioChan:  make(chan []byte, 100),
		AgentAudioChan: make(chan []byte, 100),
		DoneChan:       make(chan struct{}),
	}
}

func (s *Session) Start() {
	s.State = StateActive
	fmt.Printf("[Session %s] Started actively listening as: %s\n", s.ID, s.Profile.Name)
}

func (s *Session) Terminate() {
	s.State = StateTerminated
	close(s.DoneChan)
	fmt.Printf("[Session %s] Terminated gracefully.\n", s.ID)
}
