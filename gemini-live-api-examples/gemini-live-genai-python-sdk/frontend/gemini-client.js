/**
 * GeminiClient: Handles WebSocket communication
 */
class GeminiClient {
  constructor(config) {
    this.websocket = null;
    this.onOpen = config.onOpen;
    this.onMessage = config.onMessage;
    this.onClose = config.onClose;
    this.onError = config.onError;
  }

  connect() {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    let wsUrl = `${protocol}//${window.location.host}/ws`;
    // The client tags the demo link with their own session id (?session_id=...)
    // so every call from that link can be looked up in analytics later.
    const page = new URLSearchParams(window.location.search);
    const sessionId = page.get("session_id") || page.get("session");
    // Optional ?mobile= identifies the customer in the dealership's booking
    // system. Used only for that lookup; it is not stored as the call's caller.
    const mobile = page.get("mobile") || page.get("phone");
    const qs = new URLSearchParams();
    if (sessionId) qs.set("session_id", sessionId);
    if (mobile) qs.set("mobile", mobile);
    if ([...qs].length) wsUrl += `?${qs.toString()}`;

    this.websocket = new WebSocket(wsUrl);
    this.websocket.binaryType = "arraybuffer";

    this.websocket.onopen = () => {
      if (this.onOpen) this.onOpen();
    };

    this.websocket.onmessage = (event) => {
      if (this.onMessage) this.onMessage(event);
    };

    this.websocket.onclose = (event) => {
      if (this.onClose) this.onClose(event);
    };

    this.websocket.onerror = (event) => {
      if (this.onError) this.onError(event);
    };
  }

  send(data) {
    if (this.websocket && this.websocket.readyState === WebSocket.OPEN) {
      this.websocket.send(data);
    }
  }

  sendText(text) {
    this.send(JSON.stringify({ text: text }));
  }

  sendImage(base64Data, mimeType = "image/jpeg") {
    this.send(
      JSON.stringify({
        type: "image",
        mime_type: mimeType,
        data: base64Data,
      })
    );
  }

  disconnect() {
    if (this.websocket) {
      this.websocket.close();
      this.websocket = null;
    }
  }

  isConnected() {
    return this.websocket && this.websocket.readyState === WebSocket.OPEN;
  }
}
