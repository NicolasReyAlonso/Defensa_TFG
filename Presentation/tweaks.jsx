// Tweaks for UniFrame defense deck
// Three expressive controls that reshape the feel:
//   - mood    : academic / editorial / nocturne   (palette + texture)
//   - voice   : serif / editorial / technical     (typographic personality)
//   - density : tight / balanced / generous       (rhythm + scale)

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "mood": "academic",
  "voice": "serif",
  "density": "balanced"
}/*EDITMODE-END*/;

const { useEffect } = React;

function applyTweaks(t) {
  const html = document.documentElement;
  html.setAttribute('data-mood', t.mood);
  html.setAttribute('data-voice', t.voice);
  html.setAttribute('data-density', t.density);
}

function App() {
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);

  useEffect(() => { applyTweaks(t); }, [t.mood, t.voice, t.density]);
  // apply once on first mount before any effect runs, to avoid a flash
  if (typeof window !== 'undefined' && !window.__tweaks_applied) {
    applyTweaks(t);
    window.__tweaks_applied = true;
  }

  return (
    <TweaksPanel title="UniFrame · Tweaks">
      <TweakSection label="Atmósfera" />
      <TweakRadio
        label="Mood"
        value={t.mood}
        options={['academic', 'editorial', 'nocturne']}
        onChange={(v) => setTweak('mood', v)}
      />

      <TweakSection label="Voz tipográfica" />
      <TweakSelect
        label="Voice"
        value={t.voice}
        options={[
          { value: 'serif',     label: 'Serif académico (Plex Serif)' },
          { value: 'editorial', label: 'Editorial (Instrument Serif)' },
          { value: 'technical', label: 'Manual técnico (todo mono)' },
        ]}
        onChange={(v) => setTweak('voice', v)}
      />

      <TweakSection label="Densidad" />
      <TweakRadio
        label="Ritmo"
        value={t.density}
        options={['tight', 'balanced', 'generous']}
        onChange={(v) => setTweak('density', v)}
      />
    </TweaksPanel>
  );
}

const mount = document.getElementById('tweaks-root');
ReactDOM.createRoot(mount).render(<App />);
