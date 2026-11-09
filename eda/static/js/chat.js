(function () {
  const thread = document.getElementById('chatThread');
  const prompt = document.getElementById('chatPrompt');
  const files = document.getElementById('chatFiles');
  const preview = document.getElementById('filePreview');
  const form = document.getElementById('chatForm');

  if (form) form.addEventListener('submit', function (event) {
    if (!prompt.value.trim() && !(files && files.files.length)) { event.preventDefault(); return; }
    const indicator = document.getElementById('edaChatWorking');
    if (indicator) indicator.hidden = false;
    const send = form.querySelector('.chat-send');
    if (send) { send.disabled = true; send.setAttribute('aria-busy', 'true'); }
  });

  if (thread) thread.scrollTop = thread.scrollHeight;

  function resizePrompt() {
    if (!prompt) return;
    prompt.style.height = 'auto';
    prompt.style.height = Math.min(prompt.scrollHeight, 160) + 'px';
  }
  if (prompt) {
    prompt.addEventListener('input', resizePrompt);
    prompt.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        form.requestSubmit();
      }
    });
    resizePrompt();
  }

  if (files && preview) {
    files.addEventListener('change', function () {
      preview.innerHTML = '';
      Array.from(files.files || []).forEach(function (file) {
        const chip = document.createElement('span');
        chip.className = 'chat-file-chip';
        chip.textContent = '📎 ' + file.name;
        preview.appendChild(chip);
      });
    });
  }
})();
