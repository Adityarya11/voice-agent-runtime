package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"net"
	"os"
	"time"

	gatewaypb "atherRTC/generated/gateway"
	"atherRTC/internal/bridge"

	"google.golang.org/grpc"
)

type stubGateway struct {
	gatewaypb.UnimplementedGatewayServer
	firstEvent chan time.Time
}

func (s *stubGateway) StreamAudio(stream gatewaypb.Gateway_StreamAudioServer) error {
	if _, err := stream.Recv(); err != nil {
		return err
	}
	select {
	case s.firstEvent <- time.Now():
	default:
	}
	<-stream.Context().Done()
	return nil
}

type result struct {
	Addr             string  `json:"addr"`
	ConstructMs      float64 `json:"construct_ms"`
	OpenReturnMs     float64 `json:"open_return_ms"`
	ServerReceivedMs float64 `json:"server_received_ms"`
	Error            string  `json:"error,omitempty"`
}

func ms(d time.Duration) float64 {
	return float64(d.Microseconds()) / 1000.0
}

func main() {
	host := flag.String("host", "localhost", "host part of the dialed address")
	flag.Parse()

	lis, err := net.Listen("tcp", ":0")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	port := lis.Addr().(*net.TCPAddr).Port

	stub := &stubGateway{firstEvent: make(chan time.Time, 1)}
	server := grpc.NewServer()
	gatewaypb.RegisterGatewayServer(server, stub)
	go server.Serve(lis)
	defer server.Stop()

	res := result{Addr: fmt.Sprintf("%s:%d", *host, port)}

	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	start := time.Now()
	client, err := bridge.NewClient(res.Addr)
	res.ConstructMs = ms(time.Since(start))
	if err != nil {
		res.Error = err.Error()
		emit(res)
		return
	}
	defer client.Close()

	if _, err := client.OpenSession(ctx, "bench-open"); err != nil {
		res.Error = err.Error()
		emit(res)
		return
	}
	res.OpenReturnMs = ms(time.Since(start))

	select {
	case at := <-stub.firstEvent:
		res.ServerReceivedMs = ms(at.Sub(start))
	case <-time.After(60 * time.Second):
		res.Error = "server never received START_SESSION"
	}
	emit(res)
}

func emit(r result) {
	out, _ := json.Marshal(r)
	fmt.Println(string(out))
}
