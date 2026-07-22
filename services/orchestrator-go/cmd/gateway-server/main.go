package main

import (
	"flag"
	"log"
	"net"

	"voice-runtime/orchestrator-go/internal/config"
	"voice-runtime/orchestrator-go/internal/gateway"

	gatewaypb "voice-runtime/orchestrator-go/generated/gateway"

	"google.golang.org/grpc"
)

func main() {
	profileName := flag.String("profile", "receptionist", "Agent profile YAML name")
	port := flag.String("port", ":50052", "Gateway server listen address")
	inferenceAddr := flag.String("inference", "localhost:50051", "Inference engine address")
	flag.Parse()

	profile, err := config.LoadProfile(*profileName)
	if err != nil {
		log.Fatalf("Failed to load profile: %v", err)
	}

	lis, err := net.Listen("tcp", *port)
	if err != nil {
		log.Fatalf("Failed to listen on %s: %v", *port, err)
	}

	gwServer, err := gateway.NewServer(profile, *inferenceAddr)
	if err != nil {
		log.Fatalf("Failed to initialize gateway server: %v", err)
	}
	grpcServer := grpc.NewServer()
	gatewaypb.RegisterGatewayServer(grpcServer, gwServer)

	log.Printf("Gateway server listening on %s (profile: %s, inference: %s)", *port, profile.Name, *inferenceAddr)
	if err := grpcServer.Serve(lis); err != nil {
		log.Fatalf("Gateway server failed: %v", err)
	}
}
