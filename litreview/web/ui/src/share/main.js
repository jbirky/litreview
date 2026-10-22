import { mount } from "svelte";
import "../app.css";
import Share from "./Share.svelte";

// The review's data comes inlined in the page (litreview/web/share.py), so the page needs no server.
const data = JSON.parse(document.getElementById("review-data").textContent);
mount(Share, { target: document.getElementById("app"), props: { data } });
