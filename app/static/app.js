/* ActionLoop review interface.
 *
 * Plain ES2020 + fetch. No build step, no framework: the interesting engineering
 * in this project is in the pipeline and the approval gate, and a framework would
 * only add a toolchain without changing what a reviewer can do here.
 *
 * The UI never decides anything. Every button calls an endpoint that re-checks the
 * rule server-side, so hiding or showing a button is purely cosmetic.
 */

'use strict';

const STATUS_LABELS = {
  extracted: 'Extracted — awaiting your decision',
  approved: 'Approved — ready to execute',
  rejected: 'Rejected — will never be executed',
  executed: 'Executed',
  failed: 'Execution failed — retry available',
};

const dom = {
  form: document.getElementById('transcript-form'),
  title: document.getElementById('transcript-title'),
  date: document.getElementById('transcript-date'),
  content: document.getElementById('transcript-content'),
  extract: document.getElementById('extract'),
  loadSample: document.getElementById('load-sample'),
  feedback: document.getElementById('transcript-feedback'),
  items: document.getElementById('items'),
  itemsEmpty: document.getElementById('items-empty'),
  refresh: document.getElementById('refresh'),
  googleStatus: document.getElementById('google-status'),
  connectGoogle: document.getElementById('connect-google'),
  toast: document.getElementById('toast'),
};

let items = [];

/* ------------------------------------------------------------------ helpers */

function showToast(message, isError = false) {
  dom.toast.textContent = message;
  dom.toast.style.borderColor = isError ? 'var(--bad)' : 'var(--border)';
  dom.toast.hidden = false;
  window.clearTimeout(showToast._timer);
  showToast._timer = window.setTimeout(() => { dom.toast.hidden = true; }, 4200);
}

function setFeedback(message, kind) {
  dom.feedback.textContent = message;
  dom.feedback.className = `feedback feedback--${kind}`;
  dom.feedback.hidden = !message;
}

/** Turn the API's error envelope into a readable message. */
async function api(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await response.text();
  const payload = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const error = payload && payload.error;
    const detail = error && error.details && error.details.errors
      ? ' ' + error.details.errors.map((e) => `${e.location}: ${e.message}`).join('; ')
      : '';
    throw new Error(error ? `${error.message}${detail}` : `Request failed (${response.status})`);
  }
  return payload;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function toLocalInput(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
    + `T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function formatDeadline(iso) {
  if (!iso) return 'No deadline';
  return new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

/* ------------------------------------------------------------------- render */

function statusPill(status) {
  const map = {
    approved: 'pill--warn', executed: 'pill--ok', rejected: 'pill--muted', failed: 'pill--warn',
  };
  return el('span', `pill ${map[status] || 'pill--muted'}`, STATUS_LABELS[status] || status);
}

function renderOutcomes(item) {
  if (!item.calendar_event_id && !item.gmail_draft_id && !item.last_error) return null;

  const box = el('div', 'outcome');
  if (item.calendar_event_id) {
    const row = el('div', 'outcome__row');
    row.append(el('span', 'outcome__label', 'Calendar:'));
    const value = el('span');
    value.append(document.createTextNode('event created successfully'));
    if (item.calendar_event_link) {
      value.append(document.createTextNode(' — '));
      const link = el('a', null, 'open in Google Calendar');
      link.href = item.calendar_event_link;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      value.append(link);
    }
    row.append(value);
    box.append(row);
  }
  if (item.gmail_draft_id) {
    const row = el('div', 'outcome__row');
    row.append(el('span', 'outcome__label', 'Gmail:'));
    const value = el('span');
    value.append(el('strong', 'not-sent', 'Draft created — not sent'));
    value.append(document.createTextNode(' (check your Gmail drafts folder)'));
    row.append(value);
    box.append(row);
  }
  if (item.last_error) {
    box.append(el('p', 'error-line', `Error: ${item.last_error}`));
  }
  return box;
}

function renderEditForm(item, card) {
  const form = el('div', 'item__grid');
  const inputs = {};

  const fields = [
    { key: 'title', label: 'Title', type: 'text', value: item.title },
    { key: 'owner', label: 'Owner (blank = unassigned)', type: 'text', value: item.owner || '' },
    {
      key: 'deadline',
      label: 'Deadline (blank = none)',
      type: 'datetime-local',
      value: toLocalInput(item.deadline),
    },
    {
      key: 'priority',
      label: 'Priority',
      type: 'select',
      value: item.priority,
      options: ['low', 'medium', 'high', 'urgent'],
    },
  ];

  for (const field of fields) {
    const label = el('label');
    label.append(el('span', null, field.label));
    let input;
    if (field.type === 'select') {
      input = el('select');
      for (const option of field.options) {
        const node = el('option', null, option);
        node.value = option;
        if (option === field.value) node.selected = true;
        input.append(node);
      }
    } else {
      input = el('input');
      input.type = field.type;
      input.value = field.value;
    }
    inputs[field.key] = input;
    label.append(input);
    form.append(label);
  }

  const descriptionField = el('label');
  descriptionField.append(el('span', null, 'Description'));
  const descriptionInput = el('textarea');
  descriptionInput.rows = 3;
  descriptionInput.value = item.description || '';
  descriptionField.append(descriptionInput);
  form.append(descriptionField);
  inputs.description = descriptionInput;

  card.append(form);

  const actions = el('div', 'item__actions');
  const save = el('button', 'btn btn--ok', 'Save changes');
  save.type = 'button';
  save.addEventListener('click', async () => {
    const payload = {
      title: inputs.title.value,
      description: inputs.description.value.trim() === '' ? null : inputs.description.value,
      owner: inputs.owner.value.trim() === '' ? null : inputs.owner.value.trim(),
      deadline: inputs.deadline.value ? new Date(inputs.deadline.value).toISOString() : null,
      priority: inputs.priority.value,
    };
    try {
      await api('PATCH', `/api/action-items/${item.id}`, payload);
      showToast('Changes saved. The item is still awaiting approval.');
      await refresh();
    } catch (error) {
      showToast(error.message, true);
    }
  });

  const cancel = el('button', 'btn btn--ghost', 'Cancel');
  cancel.type = 'button';
  cancel.addEventListener('click', () => render());

  actions.append(save, cancel);
  card.append(actions);
}

function buildActions(item) {
  const actions = el('div', 'item__actions');

  const act = (label, className, handler) => {
    const button = el('button', `btn ${className}`, label);
    button.type = 'button';
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        await handler();
      } catch (error) {
        showToast(error.message, true);
      } finally {
        button.disabled = false;
      }
    });
    return button;
  };

  if (item.status === 'extracted') {
    actions.append(
      act('Edit', 'btn--ghost', () => { render(item.id); }),
      act('Approve', 'btn--primary', async () => {
        await api('POST', `/api/action-items/${item.id}/approve`);
        showToast('Approved. Nothing exists in Google yet — execute it when ready.');
        await refresh();
      }),
      act('Reject', 'btn--bad', async () => {
        await api('POST', `/api/action-items/${item.id}/reject`);
        showToast('Rejected. This item can no longer be executed.');
        await refresh();
      }),
    );
  }

  if (item.status === 'approved') {
    actions.append(act('Execute approved item', 'btn--primary', async () => {
      await api('POST', `/api/action-items/${item.id}/execute`);
      showToast('Executed. See the Calendar and Gmail result on the card.');
      await refresh();
    }));
  }

  if (item.status === 'failed') {
    actions.append(
      act('Retry execution', 'btn--primary', async () => {
        await api('POST', `/api/action-items/${item.id}/execute`);
        showToast('Retry complete (duplicate resources are prevented).');
        await refresh();
      }),
      act('Edit', 'btn--ghost', () => { render(item.id); }),
      act('Reject', 'btn--bad', async () => {
        await api('POST', `/api/action-items/${item.id}/reject`);
        await refresh();
      }),
    );
  }

  return actions.children.length ? actions : null;
}

function render(editingId = null) {
  dom.items.replaceChildren();
  dom.itemsEmpty.hidden = items.length > 0;

  for (const item of items) {
    const card = el('article', 'item');
    card.dataset.status = item.status;

    const head = el('div', 'item__head');
    head.append(el('h3', 'item__title', item.title));
    head.append(statusPill(item.status));
    card.append(head);

    const meta = el('div', 'item__meta');
    meta.append(el('span', 'pill pill--muted', `Owner: ${item.owner || 'unassigned'}`));
    meta.append(el('span', 'pill pill--muted', `Deadline: ${formatDeadline(item.deadline)}`));
    meta.append(el('span', 'pill pill--muted', `Priority: ${item.priority}`));
    meta.append(el('span', 'pill pill--muted', `Confidence: ${Math.round(item.confidence * 100)}%`));
    card.append(meta);

    if (item.description) card.append(el('p', 'item__desc', item.description));
    if (item.source_context) {
      card.append(el('p', 'item__context', `From the transcript: “${item.source_context}”`));
    }

    if (editingId === item.id) {
      renderEditForm(item, card);
    } else {
      const actions = buildActions(item);
      if (actions) card.append(actions);
    }

    const outcomes = renderOutcomes(item);
    if (outcomes) card.append(outcomes);

    dom.items.append(card);
  }
}

/* --------------------------------------------------------------------- data */

async function refresh() {
  const payload = await api('GET', '/api/action-items?limit=200');
  items = payload.items;
  render();
}

async function refreshGoogleStatus() {
  try {
    const status = await api('GET', '/auth/google/status');
    if (!status.configured) {
      dom.googleStatus.textContent = 'Google: not configured';
      dom.googleStatus.className = 'pill pill--muted';
    } else if (status.authenticated) {
      dom.googleStatus.textContent = status.connected_email
        ? `Google: ${status.connected_email}`
        : 'Google: connected';
      dom.googleStatus.className = 'pill pill--ok';
    } else {
      dom.googleStatus.textContent = 'Google: not connected';
      dom.googleStatus.className = 'pill pill--warn';
    }
    dom.connectGoogle.textContent = status.authenticated ? 'Reconnect Google' : 'Connect Google';
  } catch (error) {
    dom.googleStatus.textContent = 'Google: unavailable';
    dom.googleStatus.className = 'pill pill--muted';
  }
}

/* ------------------------------------------------------------------- events */

dom.form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!dom.content.value.trim()) {
    setFeedback('Paste a transcript first.', 'error');
    return;
  }
  dom.extract.disabled = true;
  setFeedback('Sending the transcript to Claude and validating the structured output…', 'ok');
  try {
    const payload = await api('POST', '/api/action-items/extract', {
      transcript: dom.content.value,
      title: dom.title.value || null,
      meeting_date: dom.date.value
        ? new Date(`${dom.date.value}T09:00:00`).toISOString()
        : null,
    });
    setFeedback(`Extracted ${payload.item_count} action item(s). Review them on the right.`, 'ok');
    await refresh();
  } catch (error) {
    setFeedback(error.message, 'error');
  } finally {
    dom.extract.disabled = false;
  }
});

dom.loadSample.addEventListener('click', async () => {
  try {
    const payload = await api('GET', '/api/examples/transcript');
    dom.content.value = payload.content;
    dom.title.value = 'Q3 Benchmark Review - Data Platform Sync';
    dom.date.value = '2026-09-18';
    setFeedback('Sample transcript loaded. Press “Extract action items”.', 'ok');
  } catch (error) {
    setFeedback(error.message, 'error');
  }
});

dom.refresh.addEventListener('click', async () => {
  await refresh();
  await refreshGoogleStatus();
});

dom.connectGoogle.addEventListener('click', async () => {
  try {
    const payload = await api('GET', '/auth/google');
    window.location.href = payload.authorization_url;
  } catch (error) {
    showToast(error.message, true);
  }
});

/* -------------------------------------------------------------------- start */

(async function start() {
  const params = new URLSearchParams(window.location.search);
  if (params.get('google') === 'connected') {
    showToast('Google account connected.');
    window.history.replaceState({}, '', '/');
  }
  dom.date.value = new Date().toISOString().slice(0, 10);
  await refresh();
  await refreshGoogleStatus();
}());