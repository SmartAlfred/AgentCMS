/** Nocturne design system — Tailwind v3 preset for the chat app (dark-first).
 *  Usage: module.exports = { presets: [require('./tailwind.config.js')], ... }
 *  Tailwind v4 users: map the same values into @theme in nocturne.css. */
module.exports = {
  darkMode: 'class', // app is dark by default: put `class="dark"` on <html>
  theme: {
    extend: {
      colors: {
        n: { 0:'#FFFFFF', 50:'#F4F6FA', 100:'#E6E9F2', 200:'#CDD3E0', 300:'#A8AFC0', 400:'#7E8798',
             500:'#5C6474', 600:'#414856', 700:'#2B303C', 800:'#1B1E2A', 850:'#14161F', 900:'#0E1017',
             950:'#08090D', 1000:'#05060A' },
        brand: { 300:'#A794FF', 400:'#8F75FF', 500:'#7C5CFF', 600:'#6A48F0', 700:'#5535CE', 800:'#3D2596', 900:'#241363', DEFAULT:'#7C5CFF' },
        accent: { cyan:'#22D3EE', magenta:'#F472B6', lime:'#A3E635' },
        success:'#34D399', warning:'#FBBF24', danger:'#FB7185', info:'#38BDF8',
        // semantic aliases — prefer these in app code
        base:'#08090D', sunken:'#05060A',
        surface: { DEFAULT:'#0E1017', 1:'#0E1017', 2:'#14161F', 3:'#1B1E2A', 4:'#2B303C' },
        content: { primary:'#F4F6FA', secondary:'#A8AFC0', tertiary:'#7E8798', disabled:'#5C6474', brand:'#A794FF', onBrand:'#0B0714' },
        presence: { online:'#34D399', away:'#FBBF24', busy:'#FB7185', offline:'#5C6474' },
      },
      borderColor: { DEFAULT:'rgba(255,255,255,0.10)', subtle:'rgba(255,255,255,0.06)', strong:'rgba(255,255,255,0.16)', brand:'rgba(124,92,255,0.55)' },
      backgroundImage: {
        aurora:'linear-gradient(135deg,#7C5CFF 0%,#22D3EE 100%)',
        'gradient-brand':'linear-gradient(135deg,#8F75FF 0%,#6A48F0 100%)',
        'brand-deep':'linear-gradient(160deg,#6A48F0 0%,#3D2596 100%)',
        'own-bubble':'linear-gradient(160deg,#7C5CFF 0%,#6244E8 100%)',
        'text-fade':'linear-gradient(92deg,#FFFFFF 0%,#A8AFC0 100%)',
        'text-brand':'linear-gradient(92deg,#A794FF 0%,#22D3EE 100%)',
        'glow-top':'radial-gradient(120% 80% at 50% -20%,rgba(124,92,255,0.28) 0%,rgba(124,92,255,0) 60%)',
        'glow-corner':'radial-gradient(60% 60% at 100% 0%,rgba(34,211,238,0.16) 0%,rgba(34,211,238,0) 70%)',
        shimmer:'linear-gradient(90deg,rgba(255,255,255,0) 0%,rgba(255,255,255,0.06) 50%,rgba(255,255,255,0) 100%)',
        scrim:'linear-gradient(180deg,rgba(8,9,13,0) 0%,rgba(8,9,13,0.9) 70%,#08090D 100%)',
      },
      fontFamily: {
        sans:['Inter','Inter var','-apple-system','BlinkMacSystemFont','Segoe UI','Roboto','sans-serif'],
        display:['Inter Tight','Inter','sans-serif'],
        wordmark:['Space Grotesk','Inter','sans-serif'],
        mono:['JetBrains Mono','ui-monospace','SFMono-Regular','Menlo','monospace'],
      },
      fontSize: {
        micro:['11px',{lineHeight:'16px',letterSpacing:'0.06em',fontWeight:'500'}],
        caption:['12px',{lineHeight:'16px',fontWeight:'500'}],
        'body-sm':['13px',{lineHeight:'20px'}],
        body:['15px',{lineHeight:'24px'}],
        'title-md':['17px',{lineHeight:'24px',fontWeight:'600'}],
        'title-lg':['20px',{lineHeight:'28px',letterSpacing:'-0.011em',fontWeight:'600'}],
        'display-sm':['24px',{lineHeight:'32px',letterSpacing:'-0.02em',fontWeight:'600'}],
        'display-md':['30px',{lineHeight:'38px',letterSpacing:'-0.02em',fontWeight:'600'}],
        'display-lg':['38px',{lineHeight:'46px',letterSpacing:'-0.02em',fontWeight:'600'}],
        'display-xl':['48px',{lineHeight:'56px',letterSpacing:'-0.02em',fontWeight:'600'}],
      },
      borderRadius: { xs:'6px', sm:'10px', md:'14px', lg:'18px', xl:'24px', '2xl':'32px', bubble:'18px', tail:'6px' },
      boxShadow: {
        xs:'0 1px 2px rgba(0,0,0,0.4)', sm:'0 2px 8px rgba(0,0,0,0.36)',
        md:'0 8px 24px rgba(0,0,0,0.44)', lg:'0 16px 40px rgba(0,0,0,0.52)',
        xl:'0 32px 72px rgba(0,0,0,0.60)',
        'glow-brand':'0 8px 28px rgba(124,92,255,0.36)', 'glow-cyan':'0 8px 28px rgba(34,211,238,0.28)',
        'inner-top':'inset 0 1px 0 rgba(255,255,255,0.08)', ring:'inset 0 0 0 1px rgba(255,255,255,0.08)',
        focus:'0 0 0 2px #08090D, 0 0 0 4px rgba(124,92,255,0.75)',
        elevated:'0 8px 24px rgba(0,0,0,0.44), inset 0 1px 0 rgba(255,255,255,0.08)',
      },
      backdropBlur: { glass:'24px' },
      transitionTimingFunction: {
        standard:'cubic-bezier(0.2,0,0,1)', enter:'cubic-bezier(0,0,0.2,1)',
        exit:'cubic-bezier(0.4,0,1,1)', spring:'cubic-bezier(0.34,1.56,0.64,1)',
      },
      transitionDuration: { instant:'80ms', fast:'140ms', base:'200ms', slow:'320ms', slower:'480ms' },
      maxWidth: { message:'720px' },
      width: { sidebar:'320px', rail:'72px' },
      height: { header:'64px', composer:'56px' },
      keyframes: {
        bubbleIn: { from:{opacity:0,transform:'translateY(6px)'}, to:{opacity:1,transform:'none'} },
        popIn:    { from:{opacity:0,transform:'scale(0.96)'}, to:{opacity:1,transform:'scale(1)'} },
        shimmer:  { from:{transform:'translateX(-100%)'}, to:{transform:'translateX(100%)'} },
        typeBounce: { '0%,60%,100%':{opacity:'.35',transform:'translateY(0)'}, '30%':{opacity:'1',transform:'translateY(-3px)'} },
      },
      animation: {
        'bubble-in':'bubbleIn 200ms cubic-bezier(0,0,0.2,1) both',
        'pop-in':'popIn 140ms cubic-bezier(0.34,1.56,0.64,1) both',
        shimmer:'shimmer 1.4s linear infinite',
        typing:'typeBounce 1.1s linear infinite',
      },
    },
  },
  plugins: [],
};
