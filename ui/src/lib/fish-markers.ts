/**
 * Fish Audio inline control markers.
 *
 * Emotion, tone, sound and fine-grained pronunciation control in Fish are NOT
 * API parameters — they are markup inside the text you send. That means the
 * exact same string works in the Voice Studio preview and in a live Dograh
 * call, and it means the LLM has to emit them for the agent to use them.
 *
 * Syntax differs by model family:
 *   - s2.1-pro / s2.1-pro-free / s2-pro -> [square brackets], free-form natural
 *     language allowed (e.g. [slightly sad], [warm and happy])
 *   - s1 (legacy)                       -> (parentheses), fixed tag set only
 *
 * Source: docs.fish.audio/developer-guide/core-features/emotions and
 * .../fine-grained-control
 */

export type MarkerFamily = 's2' | 's1';

export function markerFamilyFor(model: string): MarkerFamily {
  return model === 's1' ? 's1' : 's2';
}

/** Render a tag in the syntax the selected model actually understands. */
export function renderMarker(tag: string, family: MarkerFamily): string {
  return family === 's1' ? `(${tag})` : `[${tag}]`;
}

export interface MarkerGroup {
  label: string;
  hint: string;
  tags: { tag: string; note: string }[];
}

export const EMOTION_GROUPS: MarkerGroup[] = [
  {
    label: 'Core',
    hint: 'Put these at the start of a sentence — that is where they bind.',
    tags: [
      { tag: 'happy', note: 'Cheerful, upbeat' },
      { tag: 'calm', note: 'Peaceful, measured' },
      { tag: 'confident', note: 'Assertive — the default for a sales line' },
      { tag: 'empathetic', note: 'Understanding, caring' },
      { tag: 'curious', note: 'Inquisitive' },
      { tag: 'excited', note: 'Energetic' },
      { tag: 'grateful', note: 'Thankful' },
      { tag: 'relaxed', note: 'Casual' },
      { tag: 'satisfied', note: 'Content, pleased' },
      { tag: 'nervous', note: 'Anxious, uncertain' },
      { tag: 'sad', note: 'Melancholic' },
      { tag: 'worried', note: 'Concerned' },
    ],
  },
  {
    label: 'Extended',
    hint: 'Use sparingly — one primary emotion per sentence reads best.',
    tags: [
      { tag: 'sympathetic', note: 'Condolences' },
      { tag: 'compassionate', note: 'Deep care' },
      { tag: 'hopeful', note: 'Optimistic about the future' },
      { tag: 'optimistic', note: 'Positive outlook' },
      { tag: 'determined', note: 'Resolved' },
      { tag: 'uncertain', note: 'Doubtful' },
      { tag: 'confused', note: 'Puzzled' },
      { tag: 'disappointed', note: 'Let down' },
      { tag: 'regretful', note: 'Sorry, remorseful' },
      { tag: 'proud', note: 'Accomplished' },
      { tag: 'surprised', note: 'Amazed' },
      { tag: 'indifferent', note: 'Neutral' },
    ],
  },
  {
    label: 'Tone',
    hint: 'Shapes delivery rather than emotion. Can go anywhere in the line.',
    tags: [
      { tag: 'emphasis', note: 'Stress the word that follows' },
      { tag: 'soft tone', note: 'Gentle, quiet' },
      { tag: 'whispering', note: 'Very soft' },
      { tag: 'in a hurry tone', note: 'Rushed, urgent' },
      { tag: 'shouting', note: 'Loud' },
    ],
  },
  {
    label: 'Sounds & pauses',
    hint: 'Add a little text after a sound marker (e.g. "Heh, heh") for realism.',
    tags: [
      { tag: 'break', note: 'Short pause' },
      { tag: 'long-break', note: 'Extended pause' },
      { tag: 'chuckling', note: 'Light laugh' },
      { tag: 'laughing', note: 'Full laugh' },
      { tag: 'sighing', note: 'Exhale' },
      { tag: 'clear throat', note: 'Ahem' },
      { tag: 'gasping', note: 'Sharp intake' },
      { tag: 'yawning', note: 'Tired' },
    ],
  },
];

/**
 * Paralanguage effects. These use (parentheses) in BOTH model families — they
 * are a separate mechanism from the emotion cues above, which is easy to get
 * wrong. Marked experimental by Fish; the laugh/cough/sigh family often needs
 * repeating to land.
 */
export const PARALANGUAGE_TAGS: { tag: string; note: string }[] = [
  { tag: 'break', note: 'Short pause' },
  { tag: 'long-break', note: 'Extended pause' },
  { tag: 'breath', note: 'Audible breath' },
  { tag: 'sigh', note: 'Sigh (experimental)' },
  { tag: 'laugh', note: 'Laugh (experimental)' },
  { tag: 'cough', note: 'Cough (experimental)' },
  { tag: 'lip-smacking', note: 'Lip smack (experimental)' },
];

export const PHONEME_OPEN = '<|phoneme_start|>';
export const PHONEME_CLOSE = '<|phoneme_end|>';

/** Wrap an Arpabet string in Fish's phoneme delimiters. */
export function renderPhoneme(arpabet: string): string {
  return `${PHONEME_OPEN}${arpabet.trim()}${PHONEME_CLOSE}`;
}

/**
 * Ready-made English pronunciations, in CMU Arpabet with stress digits.
 * English phoneme control replaces exactly ONE word per tag.
 *
 * The carrier and product names are the ones that actually get mangled on
 * insurance calls — that is the whole reason this control exists.
 */
export const ARPABET_PRESETS: { word: string; arpabet: string; note: string }[] = [
  { word: 'engineer', arpabet: 'EH1 N JH AH0 N IH1 R', note: 'Fish docs example' },
  { word: 'insurance', arpabet: 'IH0 N SH UH1 R AH0 N S', note: 'Stress on the second syllable' },
  { word: 'deductible', arpabet: 'D IH0 D AH1 K T AH0 B AH0 L', note: '' },
  { word: 'premium', arpabet: 'P R IY1 M IY0 AH0 M', note: '' },
  { word: 'Medicare', arpabet: 'M EH1 D IH0 K EH2 R', note: '' },
  { word: 'indemnity', arpabet: 'IH0 N D EH1 M N IH0 T IY0', note: '' },
  { word: 'Aetna', arpabet: 'EH1 T N AH0', note: 'Carrier name — silent A' },
  { word: 'Cigna', arpabet: 'S IH1 G N AH0', note: 'Carrier name' },
];

export const ARPABET_REFERENCE_URL =
  'https://docs.fish.audio/developer-guide/core-features/fine-grained-control/english';

/**
 * Strip every Fish control marker, leaving the words that will actually be
 * spoken. Used for the character counter and the "plain text" view, so you can
 * see how much of your script is markup.
 */
export function stripMarkers(text: string): string {
  return text
    .replace(new RegExp(`${escapeRegex(PHONEME_OPEN)}[\\s\\S]*?${escapeRegex(PHONEME_CLOSE)}`, 'g'), '')
    .replace(/\[[^\]\n]{1,60}\]/g, '')
    .replace(/\((?:break|long-break|breath|sigh|laugh|cough|lip-smacking)\)/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function escapeRegex(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Prompt block to paste into a Dograh agent's globalNode so the LLM emits
 * markers instead of you hand-writing them. Kept here so the Studio can show
 * exactly what the agent needs — the markers are useless in production unless
 * the model is told to produce them.
 */
export const AGENT_PROMPT_BLOCK = `## Voice delivery markers

Your replies are spoken by Fish Audio. Shape delivery with inline markers.

- Start a sentence with a bracketed emotion when the emotion is not obvious
  from the words: [confident], [empathetic], [curious], [calm].
- Use [break] for a natural beat, and [emphasis] immediately before the single
  word that carries the point.
- One emotion per sentence. Never more than two markers in a row. Most
  sentences should carry no marker at all — constant emotion tagging sounds
  theatrical, not human.
- Never mention, spell out, or read a marker aloud. They are stage directions.

Example:
[empathetic] I understand, that is a lot to take in. [break] The part that
matters most is the [emphasis] daily benefit amount.`;
