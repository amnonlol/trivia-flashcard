# Difficulty rubric

The one definition of `easy` / `medium` / `hard` for this bank. It governs **both**
question paths — `generate_questions.py` templates and the agent-authored
`curated_questions.json` — because the app mixes them in a single adaptive pool
(`sampleAdaptive`, `app/src/components/Main/index.js`) and trusts the label absolutely.

Before this rubric existed the two paths used incompatible scales: templates computed
difficulty from a page-length proxy (so obscure subjects landed in `easy`), while six
independent authoring agents each guessed their own hard/medium threshold (so headline
plot facts landed in `hard`). Anything that grades a question must grade it against
this file.

## The question a grader answers

> Of the people who have finished One Piece and are sitting at a trivia night,
> **what share picks the right option** — given these four options, not given a blank page?

That framing matters. Difficulty is a property of the **whole item** (stem + the four
options), never of the fact alone. A famously obscure fact with three absurd distractors
is an easy question. A well-known fact with three genuinely competing options is not.

| tier | expected correct | what it looks like |
|---|---|---|
| **easy** | ≥ 80% | Headline facts about the Straw Hats, the Emperors, the Admirals, the major antagonists. Luffy's Devil Fruit. Zoro's dream. Who Ace was to Luffy. Which crew Nami sails with. Anyone who watched the arc remembers it without effort. |
| **medium** | 40–80% | Recurring supporting cast, arc-level plot beats, islands you spend a whole arc on, the mechanics of a well-known system. Requires having paid attention to that arc, but not study. |
| **hard** | < 40% | One-off details, exact figures, minor-character names, precise in-world terminology, the kind of thing a fan looks up to settle an argument. |

Anchors, in the bank's own words:

- **easy** — "Which Devil Fruit did Luffy eat?"; "Which Emperor did the Luffy–Law alliance set out to topple?" (Kaido); "What records reveal the Void Century?" (Poneglyphs).
- **medium** — "Which former pirate trained Luffy on Rusukaina?" (Rayleigh); "What is the name of Law's polar-bear navigator?" (Bepo); "In which village did Usopp grow up?" (Syrup Village).
- **hard** — "In which grove of Sabaody was the Human Auctioning House?" (Grove 1); "Which former Amazon Lily empress advises Hancock?" (Gloriosa); "What did Cricket, Masira and Shoujou call their partnership?" (Saruyama Alliance).

Two calibration errors to steer away from specifically, both real:

- Do **not** rate a fact hard just because its subject is minor. *"Which crew is Wanda
  affiliated with?"* shipped as `easy` purely because her wiki page is long. Page length
  is a proxy for fame, and a bad one.
- Do **not** rate a fact hard just because the stem is long or narratively framed. The
  curated questions average 117 characters against the templates' 41; the extra prose is
  style, not difficulty, and it frequently *lowers* difficulty by describing the answer.

## The two axes

A grader returns these **separately**, and they combine — they are not the same judgement.

1. **Knowledge difficulty** (0–100). How obscure is the fact itself, ignoring the options?
   0 = every viewer knows it, 100 = almost nobody does.
2. **Guessability** (boolean + why). Could someone who does *not* know the fact still pick
   the right option, from the stem, the option set, or general reasoning alone?

**Final difficulty is knowledge difficulty capped by guessability.** A guessable item
cannot be `hard` no matter how obscure the underlying fact is, because the player is not
being tested on the fact.

## This does not count as hard

Each of these makes an item answerable without knowing the answer. They are detected
mechanically by `calibrate_signals.py`, and graders are asked to flag them too.

- **Stem leak** — the stem contains the answer, including in translation. *"Kin'emon can
  instantly create **clothing**"* → `Fuku Fuku no Mi` (*fuku* = clothing). *"Leo stitches
  things with **needle and thread**"* → `Nui Nui no Mi` (*nui* = to sew).
- **Only one real option** — the other three are invented. *"Which fruit freezes the
  ocean?"* → `Hie Hie no Mi` against `Yuki Yuki no Mi`, `Samu Samu no Mi`, `Mizu Mizu no
  Mi`, none of which exist. The player picks the one they've heard of.
- **Cross-domain distractors** — the distractors belong to a different system than the
  answer. *"Which of the six Rokushiki lets you kick off the air?"* → `Geppo` against
  `Ryuo`, `Busoshoku`, `Haoshoku`, which are all Haki. The stem names the category.
- **Numeric ladder** — all four options are numbers on a scale, so it is a 1-in-4 guess
  dressed as knowledge, and round-number bias usually picks the winner. Every "hard"
  Bounties item in the curated set is one of these.
- **Near-synonym set** — the distractors are register variants of the answer.
  `Snake Princess` vs. `Serpent Queen` / `Warrior Queen` / `Lily Empress` is a coin-flip
  on phrasing, not a knowledge check.
- **Only one plausible option** — the rest are absurd for the subject. *"How did Mr. 3 try
  to kill the giants?"* → a wax candle, against a dinosaur stampede and a volcanic eruption.
- **Off-domain option** — an option from outside One Piece entirely (`Tibet`, `Outer
  space` both really shipped), which collapses the item to 1-in-3 or 1-in-2.
- **Answer-length outlier** — the correct option is conspicuously longer and more specific
  than the others, the oldest multiple-choice tell there is.

## Preferring repair over demotion

When an item is only easy because its **options** are broken, the first move is to repair
the option set — swap in real canon entities of the same type as the answer — and keep the
item at its knowledge difficulty. Demote only when the stem itself gives the answer away
and no option swap can fix it. The bank needs its hard tier to stay populated with items
that are genuinely hard, not merely labelled that way.
