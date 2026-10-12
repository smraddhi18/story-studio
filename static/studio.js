document.addEventListener('DOMContentLoaded', () => {
  const panel = document.querySelector('[data-job-id]');
  if (!panel) return;
  const id = panel.dataset.jobId;
  const render = (job) => {
    document.querySelector('#job-status').textContent = job.status;
    document.querySelector('#job-count').textContent = `${job.current} / ${job.total} steps`;
    document.querySelector('#job-progress').style.width = `${job.total ? Math.min(100, job.current / job.total * 100) : 0}%`;
    document.querySelector('#job-stage').textContent = job.stage || 'Waiting for worker';
    const error = document.querySelector('#job-error');
    error.textContent = job.error || '';
    error.hidden = !job.error;
    document.querySelector('#job-usage').textContent = job.usage.total_tokens ? `${job.usage.total_tokens.toLocaleString()} Gemini tokens used` : '';
    document.querySelector('#retry-form').hidden = !['failed', 'blocked'].includes(job.status);
    if (['succeeded', 'failed', 'blocked'].includes(job.status)) clearInterval(timer);
  };
  const poll = async () => {
    try {
      const response = await fetch(`/api/jobs/${id}`);
      if (response.ok) render(await response.json());
    } catch (_) { /* Keep the page usable while the local server reconnects. */ }
  };
  const timer = setInterval(poll, 2000);
  poll();
});
