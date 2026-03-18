import React from "react";
import ReactDOM from "react-dom/client";
import { withStreamlitConnection } from "streamlit-component-lib";
import App from "./App";

const ConnectedApp = withStreamlitConnection(App);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConnectedApp />
  </React.StrictMode>
);
