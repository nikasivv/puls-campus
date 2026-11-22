/* Progressive enhancement. All authorisation and validation also happen on the server. */
document.addEventListener('DOMContentLoaded', () => {
  const kind = document.querySelector('#grade-kind');
  const grade = document.querySelector('#grade-value');
  if (kind && grade) {
    const updateGradeOptions = () => {
      const values = kind.value === 'Зачёт' ? ['Зачёт', 'Незачёт'] : ['5', '4', '3', '2'];
      grade.replaceChildren(...values.map(value => {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = value;
        return option;
      }));
    };
    kind.addEventListener('change', updateGradeOptions);
    updateGradeOptions();
  }
  const requestType = document.querySelector('#type');
  const comment = document.querySelector('#comment');
  if (requestType && comment) {
    const updateComment = () => {
      const needsDescription = !['Справка об обучении', 'Справка для военкомата', ''].includes(requestType.value);
      comment.required = needsDescription;
      comment.minLength = needsDescription ? 5 : 0;
    };
    requestType.addEventListener('change', updateComment);
    updateComment();
  }
});

// Context-aware one-day corrections: cancellation needs a reason only; replace/extra need class details.
document.addEventListener('DOMContentLoaded', () => {
  const action = document.querySelector('#exception-action');
  const fields = document.querySelector('#exception-fields');
  if (action && fields) {
    const sync = () => {
      const visible = action.value !== 'cancel';
      fields.hidden = !visible;
      fields.querySelectorAll('input, textarea').forEach(input => { input.required = visible; });
    };
    action.addEventListener('change', sync);
    sync();
  }
  document.querySelectorAll('.bell-menu').forEach(bell => {
    document.addEventListener('click', e => { if (!bell.contains(e.target)) bell.open = false; });
    bell.addEventListener('keydown', e => { if (e.key === 'Escape') bell.open = false; });
  });
});

// Native accessible lesson detail: a modal keeps dense data readable even in a 7-day grid.
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('[data-lesson-dialog]').forEach(button => {
    const dialog = document.getElementById(button.dataset.lessonDialog);
    if (!dialog) return;
    button.addEventListener('click', () => {
      if (typeof dialog.showModal === 'function') dialog.showModal();
      else dialog.setAttribute('open', '');
    });
    dialog.querySelectorAll('[data-lesson-dialog-close]').forEach(close => {
      close.addEventListener('click', () => dialog.close());
    });
    dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
  });
});
