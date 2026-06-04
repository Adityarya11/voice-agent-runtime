package config

import (
	"fmt"
	"os"
	"path/filepath"

	"github.com/goccy/go-yaml"
)

type AgentProfile struct {
	Name         string `yaml:"name"`
	Description  string `yaml:"description"`
	SystemPrompt string `yaml:"system_prompt"`
}

func LoadProfile(profileName string) (*AgentProfile, error) {
	path := filepath.Join("..", "..", "configs", "agent_profiles", fmt.Sprintf("%s.yaml", profileName))

	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("failed to read the profile %s: %v", profileName, err)
	}

	var profile AgentProfile
	if err := yaml.Unmarshal(data, &profile); err != nil {
		return nil, fmt.Errorf("failed to parse profile %s: %v", profileName, err)
	}

	return &profile, nil
}
