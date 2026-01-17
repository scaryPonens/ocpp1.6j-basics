#!/usr/bin/env python3
"""
Simple WebSocket client that connects to a server, sends a JSON payload, and prints the response.
"""
import asyncio
import json
import websockets

# Hardcoded JSON payload to send
PAYLOAD = {
    "action": "test",
    "data": {
        "user": "demo",
        "timestamp": "2024-01-17T12:00:00Z",
        "message": "Hello from WebSocket client"
    }
}


async def main():
    """Connect to WebSocket server, send message, receive response, and exit."""
    uri = "ws://localhost:8765"
    
    print(f"Connecting to {uri}...")
    
    try:
        async with websockets.connect(uri) as websocket:
            print("Connected!")
            
            # Send the hardcoded JSON payload
            message = json.dumps(PAYLOAD)
            print(f"\nSending: {message}")
            await websocket.send(message)
            
            # Receive and print the response
            response = await websocket.recv()
            print(f"\nReceived: {response}")
            
            # Parse and pretty-print the response
            try:
                response_data = json.loads(response)
                print("\nParsed response:")
                print(json.dumps(response_data, indent=2))
            except json.JSONDecodeError:
                print("\nResponse is not valid JSON")
            
            print("\nConnection closed.")
            
    except ConnectionRefusedError:
        print("Error: Could not connect to the server. Is it running?")
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    asyncio.run(main())
