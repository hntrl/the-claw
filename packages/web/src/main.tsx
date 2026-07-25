import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import SpriteMoodPage from "./pages/SpriteMoodPage";
import "./styles.css";

const path = window.location.pathname.replace(/\/+$/, "") || "/";
const isMoodPage = path === "/sprite-moods";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {isMoodPage ? <SpriteMoodPage /> : <App />}
  </React.StrictMode>,
);
