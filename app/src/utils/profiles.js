// Local, passwordless user profiles + per-profile quiz progress, all persisted
// in localStorage. This is the single source of truth for "who is playing" and
// "what do they already know" — the app stays a static PWA (no backend), so
// everything here lives only in the current browser.

const PROFILES_KEY = 'op_profiles';
const ACTIVE_KEY = 'op_activeProfileId';
const PROGRESS_PREFIX = 'op_progress_';

// A question retires from a profile's pool after this many correct answers in a
// row. A wrong (or "I don't know") answer resets the streak, so a lucky guess
// alone can never master a question.
export const MASTERY_STREAK = 2;

// Distinct, readable colors for the profile avatar/chip. Picked round-robin so
// the first few profiles are easy to tell apart at a glance.
const COLORS = [
  '#e03997', // pink
  '#2185d0', // blue
  '#21ba45', // green
  '#f2711c', // orange
  '#6435c9', // violet
  '#00b5ad', // teal
  '#db2828', // red
  '#fbbd08', // yellow
];

// --- low-level storage helpers ------------------------------------------------

const read = (key, fallback) => {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? fallback : JSON.parse(raw);
  } catch (e) {
    return fallback;
  }
};

const write = (key, value) => {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch (e) {
    // Storage full or disabled (private mode) — progress just won't persist.
  }
};

const uid = () =>
  `p_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;

// --- stable question identity -------------------------------------------------

// Questions in the bank have no id, so we derive a stable one from the question
// text (djb2). The same text always hashes to the same id, so Main (selection)
// and Quiz (recording) agree without touching questions.json's schema.
export const questionId = question => {
  const text = String(question || '');
  let hash = 5381;
  for (let i = 0; i < text.length; i += 1) {
    hash = (hash * 33) ^ text.charCodeAt(i);
  }
  // >>> 0 turns it into an unsigned 32-bit int; base36 keeps the key short.
  return `q_${(hash >>> 0).toString(36)}`;
};

// --- profile CRUD -------------------------------------------------------------

export const listProfiles = () => read(PROFILES_KEY, []);

export const getActiveProfileId = () => {
  const id = read(ACTIVE_KEY, null);
  const profiles = listProfiles();
  // Guard against a dangling active id (e.g. profile deleted in another tab).
  if (id && profiles.some(p => p.id === id)) return id;
  return profiles.length ? profiles[0].id : null;
};

export const getActiveProfile = () => {
  const id = getActiveProfileId();
  return listProfiles().find(p => p.id === id) || null;
};

export const setActiveProfile = id => {
  write(ACTIVE_KEY, id);
  return id;
};

export const createProfile = name => {
  const profiles = listProfiles();
  const profile = {
    id: uid(),
    name: String(name).trim().slice(0, 24) || 'Player',
    color: COLORS[profiles.length % COLORS.length],
    createdAt: Date.now(),
  };
  write(PROFILES_KEY, [...profiles, profile]);
  setActiveProfile(profile.id);
  return profile;
};

export const deleteProfile = id => {
  const remaining = listProfiles().filter(p => p.id !== id);
  write(PROFILES_KEY, remaining);
  try {
    localStorage.removeItem(`${PROGRESS_PREFIX}${id}`);
  } catch (e) {
    /* ignore */
  }
  // If we deleted the active profile, fall back to the first remaining one.
  if (getActiveProfileId() === id || read(ACTIVE_KEY, null) === id) {
    setActiveProfile(remaining.length ? remaining[0].id : null);
  }
  return remaining;
};

// --- progress -----------------------------------------------------------------

const emptyProgress = () => ({
  questions: {},
  difficultyStats: {
    easy: { correct: 0, total: 0 },
    medium: { correct: 0, total: 0 },
    hard: { correct: 0, total: 0 },
  },
  // Per-category correct/total, keyed by the question's `category`. Populated
  // lazily as categories are seen, so it needs no fixed key list.
  categoryStats: {},
});

export const getProgress = id => {
  if (!id) return emptyProgress();
  const stored = read(`${PROGRESS_PREFIX}${id}`, null);
  if (!stored) return emptyProgress();
  // Merge so older/partial saves still have every expected field.
  return {
    ...emptyProgress(),
    ...stored,
    questions: stored.questions || {},
    difficultyStats: {
      ...emptyProgress().difficultyStats,
      ...(stored.difficultyStats || {}),
    },
    categoryStats: stored.categoryStats || {},
  };
};

// Record one quiz's worth of answers. `records` come straight from the Quiz's
// questionsAndAnswers, each carrying { id, difficulty, point }. Returns the
// number of questions that *newly* reached mastery this round (for UI feedback).
export const recordAnswers = (id, records = []) => {
  if (!id) return 0;
  const progress = getProgress(id);
  let newlyMastered = 0;

  records.forEach(record => {
    const qid = record.id;
    if (!qid) return;

    const prev = progress.questions[qid] || {
      seen: 0,
      correct: 0,
      wrong: 0,
      streak: 0,
      mastered: false,
    };
    const wasMastered = prev.mastered;
    const gotItRight = record.point === 1;

    const next = {
      ...prev,
      seen: prev.seen + 1,
      correct: prev.correct + (gotItRight ? 1 : 0),
      wrong: prev.wrong + (gotItRight ? 0 : 1),
      streak: gotItRight ? prev.streak + 1 : 0,
      lastSeen: Date.now(),
    };
    next.mastered = next.streak >= MASTERY_STREAK;
    if (next.mastered && !wasMastered) newlyMastered += 1;

    progress.questions[qid] = next;

    const bucket = progress.difficultyStats[record.difficulty];
    if (bucket) {
      bucket.total += 1;
      if (gotItRight) bucket.correct += 1;
    }

    // Per-category tally powers the "what should I study?" dashboard.
    if (record.category) {
      const cat = progress.categoryStats[record.category] || {
        correct: 0,
        total: 0,
      };
      cat.total += 1;
      if (gotItRight) cat.correct += 1;
      progress.categoryStats[record.category] = cat;
    }
  });

  write(`${PROGRESS_PREFIX}${id}`, progress);
  return newlyMastered;
};

// Set of question ids this profile has mastered — excluded from new draws.
export const masteredIds = id => {
  const { questions } = getProgress(id);
  return new Set(
    Object.keys(questions).filter(qid => questions[qid].mastered)
  );
};

// Question ids this profile has gotten wrong at least once and not yet mastered —
// the pool for "Review missed" drilling. Ordered for lightweight spaced
// repetition: the most-missed first, ties broken by least-recently-seen, so the
// weakest, stalest questions resurface soonest.
export const missedQuestionIds = id => {
  const { questions } = getProgress(id);
  return Object.keys(questions)
    .filter(qid => questions[qid].wrong > 0 && !questions[qid].mastered)
    .sort((a, b) => {
      const qa = questions[a];
      const qb = questions[b];
      if (qb.wrong !== qa.wrong) return qb.wrong - qa.wrong;
      return (qa.lastSeen || 0) - (qb.lastSeen || 0);
    });
};

// How many questions are available to review right now (for the button label).
export const missedCount = id => missedQuestionIds(id).length;

// Per-category accuracy for the study dashboard, most-answered first. Each entry:
// { category, correct, total, accuracy } where accuracy is a 0–100 int or null.
export const getCategoryStats = id => {
  const { categoryStats } = getProgress(id);
  return Object.keys(categoryStats)
    .map(category => {
      const { correct, total } = categoryStats[category];
      return {
        category,
        correct,
        total,
        accuracy: total ? Math.round((correct / total) * 100) : null,
      };
    })
    .sort((a, b) => b.total - a.total);
};

// Headline numbers for the home-screen summary.
export const getSummary = id => {
  const { questions, difficultyStats } = getProgress(id);
  const mastered = Object.values(questions).filter(q => q.mastered).length;
  const total = Object.values(difficultyStats).reduce(
    (sum, b) => sum + b.total,
    0
  );
  const correct = Object.values(difficultyStats).reduce(
    (sum, b) => sum + b.correct,
    0
  );
  return {
    mastered,
    answered: total,
    accuracy: total ? Math.round((correct / total) * 100) : null,
  };
};

// Difficulty mix for "Adaptive" mode, derived from rolling accuracy. Low
// accuracy (or little data) leans easy; strong accuracy leans hard. Weights are
// proportions the sampler turns into per-difficulty question counts.
export const adaptiveMix = id => {
  const { difficultyStats } = getProgress(id);
  const total = Object.values(difficultyStats).reduce(
    (sum, b) => sum + b.total,
    0
  );
  const correct = Object.values(difficultyStats).reduce(
    (sum, b) => sum + b.correct,
    0
  );

  // Not enough history yet — start people off gently.
  if (total < 5) return { easy: 0.7, medium: 0.3, hard: 0 };

  const accuracy = correct / total;
  if (accuracy < 0.5) return { easy: 0.7, medium: 0.3, hard: 0 };
  if (accuracy < 0.8) return { easy: 0.25, medium: 0.5, hard: 0.25 };
  return { easy: 0.1, medium: 0.4, hard: 0.5 };
};
