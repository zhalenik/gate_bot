'use strict';
// Shared keyboard behavior. Domain actions stay in their page modules.
document.addEventListener('keydown',event=>{
    if(event.key==='Escape')document.querySelectorAll('.account-menu[open]').forEach(menu=>{menu.open=false;menu.querySelector('summary')?.focus();});
    if(event.key==='Escape')document.querySelectorAll('.multi-select[open]').forEach(menu=>{menu.open=false;menu.querySelector('summary')?.focus();});
    const modal=[...document.querySelectorAll('.modal')].reverse().find(node=>node.style.display==='block');
    if(modal&&event.key==='Tab'){
        const nodes=[...modal.querySelectorAll('input:not([disabled]),select:not([disabled]),button:not([disabled]),a[href]')].filter(node=>node.offsetParent!==null);
        if(nodes.length){const first=nodes[0],last=nodes[nodes.length-1];if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}}
    }
    if(modal&&event.key==='Escape'&&!modal.dataset.dynamic){if(modal.id==='accessModal'&&typeof window.closeModal==='function')window.closeModal();else modal.style.display='none';}
});
document.querySelectorAll('th[onclick]').forEach(th=>{th.tabIndex=0;th.setAttribute('aria-sort','none');th.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();th.click();}});});
document.addEventListener('click',event=>{
    document.querySelectorAll('.account-menu[open]').forEach(menu=>{if(!menu.contains(event.target))menu.open=false;});
});
const activeBranchSelect=document.getElementById('activeBranchSelect');
if(activeBranchSelect)activeBranchSelect.addEventListener('change',async()=>{
    const status=document.getElementById('branchSwitchStatus');activeBranchSelect.disabled=true;status.hidden=true;
    try{
        const response=await fetch('/admin/api/identity/branch',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content},body:JSON.stringify({branch_id:activeBranchSelect.value})});
        const result=await response.json();if(!response.ok)throw new Error(result.message||'Не удалось сменить филиал');location.reload();
    }catch(error){activeBranchSelect.value=activeBranchSelect.dataset.active;status.textContent=error.message;status.hidden=false;activeBranchSelect.disabled=false;}
});
