'use strict';
window.registrationChoices = function () {
  const inputs = [...document.querySelectorAll('[data-registration-documents] input')];
  const result = {};
  for (const input of inputs) {
    if (!input.reportValidity()) return null;
    result[input.dataset.documentKind] = Number(input.value);
  }
  return result;
};
document.addEventListener('click', async event => {
  const link = event.target.closest('a[data-legal-document]');
  if (!link || event.ctrlKey || event.metaKey || event.shiftKey) return;
  event.preventDefault();
  const dialog = document.createElement('dialog'), header=document.createElement('header');
  dialog.className='legal-dialog';dialog.setAttribute('aria-label',link.dataset.title);
  const title=document.createElement('h2'), close=document.createElement('button'), content=document.createElement('article');
  title.textContent=link.dataset.title;close.textContent='Закрыть';close.type='button';close.className='btn-action';close.onclick=()=>dialog.close();
  const open=document.createElement('a');open.href=link.href;open.target='_blank';open.rel='noopener';open.textContent='Открыть документ в новой вкладке';
  header.append(title,close);dialog.append(header,content,open);document.body.append(dialog);
  dialog.addEventListener('close',()=>{dialog.remove();link.focus();},{once:true});
  dialog.showModal();
  if (link.dataset.legalDocument==='application/pdf') {
    const frame=document.createElement('iframe');frame.title=title.textContent;frame.src=link.href;content.append(frame);
  } else {
    content.textContent='Загрузка…';
    try {
      const response=await fetch(link.href);if (!response.ok) throw Error();
      content.textContent=await response.text();
    } catch {content.textContent='Не удалось загрузить документ.';}
  }
});
document.getElementById('offerUploadForm')?.addEventListener('submit', async event => {
  event.preventDefault();const form=event.currentTarget,button=form.querySelector('button');
  button.disabled=true;
  try {
    const response=await fetch(form.action,{method:'POST',headers:{'X-CSRF-Token':form.elements.csrf_token.value},body:new FormData(form)});
    if(!response.ok) throw Error((await response.json()).message||'Не удалось загрузить документ.');
    location.reload();
  } catch(error){form.querySelector('output').textContent=error.message;button.disabled=false;}
});
