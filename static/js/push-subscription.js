/* Shared by the two independently scoped GateBot service workers. */
(() => {
  'use strict';
  function applicationKey(encoded) {
    try {
      const raw=atob(encoded.replace(/-/g,'+').replace(/_/g,'/'));
      const key=Uint8Array.from(raw,character=>character.charCodeAt(0));
      if(key.length===65 && key[0]===4) return key;
    } catch {}
    throw Error('GateBot вернул некорректный открытый ключ Push. Обратитесь в поддержку.');
  }
  function sameKey(sub,key) {
    const previous=sub?.options?.applicationServerKey;
    if(!previous) return true;
    const bytes=new Uint8Array(previous);
    return bytes.length===key.length && bytes.every((value,index)=>value===key[index]);
  }
  async function subscribe(registration,encodedKey){
    const key=applicationKey(encodedKey),manager=registration.pushManager;
    let previous=await manager.getSubscription();
    if(previous){
      if(sameKey(previous,key))return previous;
      if(!await previous.unsubscribe())throw Error('Не удалось обновить прежнюю подписку. Повторите подключение.');
    }
    for(let attempt=0;attempt<2;attempt++){
      try { return await manager.subscribe({userVisibleOnly:true,applicationServerKey:key}); }
      catch(error){
        if(error.name==='NotAllowedError')throw Error('Браузер запретил регистрацию Push. Проверьте разрешение для сайта.');
        if(error.name!=='AbortError' && error.name!=='NetworkError' && !/push service error/i.test(error.message))throw error;
        if(attempt===0 && navigator.onLine!==false){
          await new Promise(resolve=>setTimeout(resolve,900));
          const registered=await manager.getSubscription();
          if(registered && sameKey(registered,key))return registered;
          continue;
        }
        throw Error('Браузер не смог зарегистрироваться в своей службе Push. Проверьте соединение или попробуйте другую сеть и нажмите «Подключить Push» ещё раз. Повторно разрешать уведомления не требуется.');
      }
    }
  }
  window.GateBotPushSubscription={subscribe,matchesKey:(subscription,key)=>sameKey(subscription,applicationKey(key))};
})();
