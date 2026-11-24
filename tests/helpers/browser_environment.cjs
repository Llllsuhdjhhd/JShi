// Browser facilities used by the page, shared by microphone/player UI mocks.
module.exports=function prepare(context){
 // Page polling should be available without keeping the Node test process alive.
 const interval=context.setInterval??setInterval;
 context.setInterval=(...args)=>{const id=interval(...args);id?.unref?.();return id;};
 context.clearInterval??=clearInterval;
 function element(node){
  node.dataset??={};
  node.addEventListener??=function(name,fn){(this.listeners??={})[name]=fn;};
  node.scrollTo??=function(value){this.scrollTop=value.top;};
  node.getBoundingClientRect??=()=>({left:0,right:800,top:0,bottom:400});
  node.scrollTop??=0;node.scrollHeight??=0;node.clientHeight??=400;
  node.classList??={add(){},remove(){}};
  node.classList.toggle??=function(name,force){const on=force??!this.contains?.(name);this[on?'add':'remove'](name);return on;};
  return node;
 }
 for(const method of ['getElementById','createElement']){
  const original=context.document[method];
  context.document[method]=(...args)=>element(original(...args));
 }
 context.document.addEventListener??=function(name,fn){(this.listeners??={})[name]=fn;};
 context.document.querySelectorAll??=()=>[];
 context.window??={addEventListener(){}};
 context.MutationObserver??=class{observe(){}};
 context.ResizeObserver??=class{observe(){}};
 context.requestAnimationFrame??=fn=>fn();
 context.matchMedia??=()=>({matches:false});
};
