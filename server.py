#!/usr/bin/env python3
"""
Simple WebSocket server that logs incoming messages and replies with static JSON.
"""
import asyncio
import json
import logging
import websockets

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Static JSON response
STATIC_RESPONSE = {
    "status": "ok",
    "message": "Message received",
    "server": "websocket-demo"
}


async def handle_client(websocket):
    """
    Handle incoming WebSocket connections.
    
    Logs all incoming messages verbatim and replies with static JSON.
    """
    client_address = websocket.remote_address
    logger.info(f"Client connected from {client_address}")
    
    try:
        async for message in websocket:
            # Log the incoming message verbatim
            logger.info(f"Received message: {message}")
            
            # Reply with static JSON message
            response = json.dumps(STATIC_RESPONSE)
            await websocket.send(response)
            logger.info(f"Sent response: {response}")
            
    except websockets.exceptions.ConnectionClosed:
        logger.info(f"Client {client_address} disconnected")
    except Exception as e:
        logger.error(f"Error handling client {client_address}: {e}")
    finally:
        logger.info(f"Connection closed for {client_address}")


async def main():
    """Start the WebSocket server."""
    host = "localhost"
    port = 8765
    
    logger.info(f"Starting WebSocket server on ws://{host}:{port}")
    
    async with websockets.serve(handle_client, host, port):
        logger.info("Server is running. Press Ctrl+C to stop.")
        await asyncio.Future()  # Run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
