'use strict';
(() => {
 const panel=document.getElementById('branchDeletionPanel');if(!panel)return;
 const dialog=document.getElementById('branchDeletionDialog'),form=document.getElementById('branchDeletionForm'),input=document.getElementById('branchDeletionConfirmation');
 const submit=document.getElementById('branchDeletionSubmit'),notice=document.getElementById('branchDeletionNotice'),open=document.getElementById('deleteBranchButton'),cancel=document.getElementById('branchDeletionCancel');
 let branchId='',preview=null,loading=0,saving=false;
 function available(){submit.disabled=saving||!preview?.can_delete||input.value!==preview.branch_id;}
 function selectBranch(value){
  const next=String(value||'');panel.hidden=!next;
  if(next===branchId)return;
  branchId=next;loading++;preview=null;
  if(dialog.open&&!saving)dialog.close();
 }
 document.addEventListener('gatebot:branch-settings-loaded',event=>selectBranch(event.detail.branchId));
 document.getElementById('newBranchBtn')?.addEventListener('click',()=>selectBranch(''));
 input.addEventListener('input',available);
 cancel.addEventListener('click',()=>dialog.close());
 open.addEventListener('click',async()=>{
  const id=branchId,version=++loading;open.disabled=true;
  try{
   const result=await api('/admin/api/branches/'+encodeURIComponent(id)+'/deletion');
   if(version!==loading||id!==branchId)return;
   preview=result.preview;input.value='';
   document.getElementById('branchDeletionSummary').textContent=`${preview.name} · ID ${preview.branch_id}. Будут удалены клиенты: ${preview.clients}, аккаунты администраторов: ${preview.admins}.`;
   notice.textContent=preview.reason||'После удаления прежние ссылки, QR и административные сессии этого филиала перестанут работать.';
   available();dialog.showModal();input.focus();
  }catch(error){showAlert(error.message,'error');}finally{open.disabled=false;}
 });
 form.addEventListener('submit',async event=>{
  event.preventDefault();if(!preview||submit.disabled||saving)return;
  saving=true;available();cancel.disabled=true;input.disabled=true;
  try{
   await post('/admin/api/branches/'+encodeURIComponent(preview.branch_id)+'/delete',{confirmation:input.value,revision:preview.revision});
   location.assign('/admin/settings');
  }catch(error){notice.textContent=error.message;preview=null;}
  finally{saving=false;input.disabled=false;cancel.disabled=false;available();}
 });
 dialog.addEventListener('cancel',event=>{if(saving)event.preventDefault();});
})();
