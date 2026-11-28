for (const form of document.querySelectorAll('.updates-form')) {
  const email = form.querySelector('input[name="email"]');
  const button = form.querySelector('button');
  const status = form.querySelector('[role="status"]');

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (button.disabled) return;

    button.disabled = true;
    status.textContent = 'Subscribing…';
    try {
      const response = await fetch(form.action, {
        method: 'POST',
        mode: 'same-origin',
        body: new URLSearchParams({ email: email.value }),
      });
      if (response.status === 204) {
        form.reset();
        status.textContent = 'You’re subscribed.';
      } else if (response.status === 400) {
        status.textContent = 'Enter a valid email address.';
      } else {
        status.textContent = 'Couldn’t subscribe. Please try again.';
      }
    } catch {
      status.textContent = 'Couldn’t subscribe. Please try again.';
    } finally {
      button.disabled = false;
    }
  });
  form.hidden = false;
}
