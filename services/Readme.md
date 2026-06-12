```
CLIENT
   |
   | START_SESSION
   |
SERVER
   |
   | session created
   |
CLIENT
   |
   | AudioChunk
   | AudioChunk
   | AudioChunk
   |
SERVER
   |
   | Transcript("hello")
   |
CLIENT
   |
   | AudioChunk
   |
SERVER
   |
   | Transcript("hello how are you")
   |
SERVER
   |
   | AudioChunk (AI voice)
   | AudioChunk (AI voice)
   |
CLIENT interrupts
   |
   | BARGE_IN
   |
SERVER
   |
   | stop speaking
   |
CLIENT
   |
   | END_SESSION
   |
SERVER
   |
   | cleanup

```
