import React, { useState } from 'react';
import PropTypes from 'prop-types';
import { Icon } from 'semantic-ui-react';

import mindImg from '../../images/mind.svg';

import { CATEGORIES, DIFFICULTY, SAGAS, APP_VERSION } from '../../constants';
import {
  shuffle,
  questionId,
  masteredIds,
  missedQuestionIds,
  getCategoryStats,
  adaptiveMix,
  getSummary,
} from '../../utils';
import { useProfile } from '../../context/ProfileContext';

import Offline from '../Offline';

// Chip choices for the setup screen. The question bank is entirely
// multiple-choice, so we drop the old "Type of Questions" selector, and the
// three-part hours/minutes/seconds countdown collapses into a few presets.
const QUESTION_COUNTS = [5, 10, 15, 20, 30];

const TIMER_OPTIONS = [
  { value: 0, label: 'No timer' },
  { value: 120, label: '2 min' },
  { value: 300, label: '5 min' },
  { value: 600, label: '10 min' },
];

// Reuse the shared constants but shorten their labels so the chips stay tidy
// on a phone screen.
const CATEGORY_OPTIONS = CATEGORIES.map(c => ({
  value: c.value,
  label: c.text === 'Any Category' ? 'Any' : c.text,
}));

const DIFFICULTY_OPTIONS = DIFFICULTY.map(d => ({
  value: d.value,
  label: d.text.replace(' Difficulty', '').replace(' (match my skill)', ''),
}));

const SAGA_OPTIONS = SAGAS.map(s => ({ value: s.value, label: s.text }));

// A wrapping row of single-select pills.
const ChipGroup = ({ options, value, onChange, disabled }) => (
  <div className="op-chips">
    {options.map(opt => (
      <button
        key={String(opt.value)}
        type="button"
        className={`op-chip${opt.value === value ? ' selected' : ''}`}
        onClick={() => onChange(opt.value)}
        disabled={disabled}
      >
        {opt.label}
      </button>
    ))}
  </div>
);

ChipGroup.propTypes = {
  options: PropTypes.array.isRequired,
  value: PropTypes.oneOfType([PropTypes.string, PropTypes.number]).isRequired,
  onChange: PropTypes.func.isRequired,
  disabled: PropTypes.bool,
};

// Given a pool and a per-difficulty weighting, pick `n` questions whose
// difficulty distribution matches the mix as closely as the pool allows. Any
// shortfall from rounding or empty buckets is filled from the rest of the pool.
const sampleAdaptive = (pool, n, mix) => {
  const buckets = { easy: [], medium: [], hard: [] };
  pool.forEach(q => {
    if (buckets[q.difficulty]) buckets[q.difficulty].push(q);
  });

  const selected = [];
  ['easy', 'medium', 'hard'].forEach(level => {
    const count = Math.round(n * (mix[level] || 0));
    selected.push(...shuffle(buckets[level]).slice(0, count));
  });

  if (selected.length < n) {
    const chosen = new Set(selected);
    const rest = shuffle(pool.filter(q => !chosen.has(q)));
    selected.push(...rest.slice(0, n - selected.length));
  }

  return shuffle(selected).slice(0, n);
};

// Fetch the local question bank once and memoize the promise so repeated
// quizzes reuse the same in-memory copy.
let questionBankPromise = null;
const loadQuestionBank = () => {
  if (!questionBankPromise) {
    questionBankPromise = fetch(`${process.env.PUBLIC_URL}/data/questions.json`)
      .then(response => {
        if (!response.ok) throw new Error('Failed to load question bank');
        return response.json();
      })
      .catch(error => {
        // Don't cache a failed fetch — allow a retry on the next click.
        questionBankPromise = null;
        throw error;
      });
  }

  return questionBankPromise;
};

const Main = ({ startQuiz }) => {
  const { activeProfile } = useProfile();
  const [category, setCategory] = useState('0');
  const [numOfQuestions, setNumOfQuestions] = useState(10);
  const [difficulty, setDifficulty] = useState('adaptive');
  // Spoiler scope: 0 = whole series (no limit); otherwise only questions whose
  // subject debuts up to and including this saga (see sagaOrder in the bank).
  const [sagaLimit, setSagaLimit] = useState(0);
  const [countdownSeconds, setCountdownSeconds] = useState(0);
  // Mock-Event mode: no per-question reveal, timed, results only at the end —
  // rehearsal for the real trivia event. Off = the instant-feedback learn mode.
  const [eventMode, setEventMode] = useState(false);
  const [processing, setProcessing] = useState(false);
  const [error, setError] = useState(null);
  const [offline, setOffline] = useState(false);

  // Shuffle each question's options and hand the batch to the quiz engine.
  // `asEvent` runs the batch as a timed mock event (no reveals until the end);
  // an event with "No timer" selected still gets a default clock so it stays a
  // real rehearsal.
  const buildAndStart = (picked, asEvent = false) => {
    const results = picked.map(element => ({
      ...element,
      options: shuffle([element.correct_answer, ...element.incorrect_answers]),
    }));
    const time = asEvent && countdownSeconds === 0 ? 300 : countdownSeconds;
    setProcessing(false);
    startQuiz(results, time, asEvent);
  };

  // Review mode: re-quiz only the questions this profile has missed, weakest and
  // stalest first (order comes from missedQuestionIds). Ignores the
  // category/difficulty pickers — the point is to drill past mistakes.
  const reviewMissed = () => {
    if (!activeProfile) return;
    setProcessing(true);
    if (error) setError(null);

    loadQuestionBank()
      .then(bank =>
        setTimeout(() => {
          const order = missedQuestionIds(activeProfile.id);
          const rank = new Map(order.map((id, i) => [id, i]));
          const pool = bank
            .filter(
              q =>
                rank.has(questionId(q.question)) &&
                (sagaLimit === 0 || !q.sagaOrder || q.sagaOrder <= sagaLimit)
            )
            .sort(
              (a, b) =>
                rank.get(questionId(a.question)) -
                rank.get(questionId(b.question))
            );

          if (pool.length === 0) {
            setProcessing(false);
            setError({
              message: (
                <span>
                  Nothing to review yet — miss a question in a normal quiz and it
                  will show up here.
                </span>
              ),
            });
            return;
          }

          buildAndStart(pool.slice(0, numOfQuestions));
        }, 800)
      )
      .catch(error =>
        setTimeout(() => {
          if (!navigator.onLine) {
            setOffline(true);
          } else {
            setProcessing(false);
            setError(error);
          }
        }, 800)
      );
  };

  const fetchData = () => {
    setProcessing(true);

    if (error) setError(null);

    // Load the local One Piece question bank (served statically, cached by the
    // service worker) instead of hitting an external trivia API. The response is
    // memoized so subsequent quizzes filter in-memory without re-fetching.
    loadQuestionBank()
      .then(bank =>
        setTimeout(() => {
          const isAdaptive = difficulty === 'adaptive';

          // Base filter: category always applies; a fixed difficulty applies
          // only when the user picked one (Adaptive & Any skip it). The saga
          // scope hides subjects that debut later than the reader has come;
          // untagged questions (no sagaOrder) can't be gated, so they always
          // show. All bank questions are multiple-choice, so there's no type
          // filter.
          const withinSaga = q =>
            sagaLimit === 0 || !q.sagaOrder || q.sagaOrder <= sagaLimit;

          const matchesQuery = q =>
            (category === '0' || q.category === category) &&
            (isAdaptive || difficulty === '0' || q.difficulty === difficulty) &&
            withinSaga(q);

          const matched = bank.filter(matchesQuery);

          // Drop questions this profile has already mastered (answered right
          // twice in a row) so we stop re-quizzing what they know.
          const mastered = activeProfile
            ? masteredIds(activeProfile.id)
            : new Set();
          const fresh = matched.filter(q => !mastered.has(questionId(q.question)));

          // If retiring mastered questions leaves too few, fall back to the
          // full matched set so a strong player can always keep playing.
          const usingFallback = fresh.length < numOfQuestions;
          const pool = usingFallback ? matched : fresh;

          if (pool.length < numOfQuestions) {
            const message = (
              <span>
                Not enough questions match your picks — only {pool.length}{' '}
                available, but you asked for {numOfQuestions}. Try a smaller
                count, a different category, or another difficulty.
              </span>
            );

            setProcessing(false);
            setError({ message });

            return;
          }

          const picked = isAdaptive
            ? sampleAdaptive(
                pool,
                numOfQuestions,
                adaptiveMix(activeProfile ? activeProfile.id : null)
              )
            : shuffle(pool).slice(0, numOfQuestions);

          buildAndStart(picked, eventMode);
        }, 800)
      )
      .catch(error =>
        setTimeout(() => {
          if (!navigator.onLine) {
            setOffline(true);
          } else {
            setProcessing(false);
            setError(error);
          }
        }, 800)
      );
  };

  if (offline) return <Offline />;

  const summary = activeProfile ? getSummary(activeProfile.id) : null;
  const categoryStats = activeProfile ? getCategoryStats(activeProfile.id) : [];
  const missed = activeProfile ? missedQuestionIds(activeProfile.id).length : 0;

  // Category the player is weakest in (lowest accuracy among those with data) —
  // flagged in the dashboard so they know where to focus. null if no data yet.
  const weakestCategory = categoryStats
    .filter(c => c.accuracy !== null)
    .reduce(
      (worst, c) => (worst === null || c.accuracy < worst.accuracy ? c : worst),
      null
    );

  return (
    <div className="op-shell">
      <div className="op-card">
        <div className="op-hero">
          <img src={mindImg} alt="" />
          <div>
            <h1>One Piece Trivia</h1>
            <p>Pick your challenge and set sail.</p>
          </div>
          <span className="op-version">v{APP_VERSION}</span>
        </div>

        {activeProfile && summary && (
          <div className="op-profile">
            <span
              className="op-badge"
              style={{ backgroundColor: activeProfile.color }}
            >
              <Icon name="user" fitted />
              {activeProfile.name}
            </span>
            <span>
              {summary.answered === 0
                ? 'No quizzes yet'
                : `${summary.mastered} mastered${
                    summary.accuracy !== null
                      ? ` · ${summary.accuracy}% accuracy`
                      : ''
                  }`}
            </span>
          </div>
        )}

        {/* Weak-area dashboard: per-category accuracy so the player knows what to
            study. The weakest category is flagged with a target icon. */}
        {categoryStats.length > 0 && (
          <div className="op-catstats">
            {categoryStats.map(({ category, accuracy, correct, total }) => (
              <div className="op-catstat" key={category}>
                <span className="op-catstat-name">
                  {weakestCategory && weakestCategory.category === category && (
                    <Icon name="target" color="red" fitted />
                  )}{' '}
                  {category}
                </span>
                <span className="op-catstat-bar">
                  <span
                    className="op-catstat-fill"
                    style={{ width: `${accuracy || 0}%` }}
                  />
                </span>
                <span className="op-catstat-pct">
                  {accuracy === null ? '—' : `${accuracy}%`}
                  <small>
                    {' '}
                    ({correct}/{total})
                  </small>
                </span>
              </div>
            ))}
          </div>
        )}

        {error && (
          <div
            className="op-feedback wrong"
            style={{ marginTop: 16, cursor: 'pointer' }}
            onClick={() => setError(null)}
            role="button"
          >
            <Icon name="warning circle" className="op-fb-icon" />
            <div>{error.message}</div>
          </div>
        )}

        <div className="op-field">
          <div className="op-field-label">
            <Icon name="folder open" fitted /> Category
          </div>
          <ChipGroup
            options={CATEGORY_OPTIONS}
            value={category}
            onChange={setCategory}
            disabled={processing}
          />
        </div>

        <div className="op-field">
          <div className="op-field-label">
            <Icon name="book" fitted /> Up to saga{' '}
            <span className="op-field-note">(avoid spoilers)</span>
          </div>
          <ChipGroup
            options={SAGA_OPTIONS}
            value={sagaLimit}
            onChange={setSagaLimit}
            disabled={processing}
          />
        </div>

        <div className="op-field">
          <div className="op-field-label">
            <Icon name="signal" fitted /> Difficulty
          </div>
          <ChipGroup
            options={DIFFICULTY_OPTIONS}
            value={difficulty}
            onChange={setDifficulty}
            disabled={processing}
          />
        </div>

        <div className="op-field">
          <div className="op-field-label">
            <Icon name="list ol" fitted /> Questions
          </div>
          <ChipGroup
            options={QUESTION_COUNTS.map(n => ({ value: n, label: n }))}
            value={numOfQuestions}
            onChange={setNumOfQuestions}
            disabled={processing}
          />
        </div>

        <div className="op-field">
          <div className="op-field-label">
            <Icon name="flag checkered" fitted /> Mode
          </div>
          <ChipGroup
            options={[
              { value: false, label: 'Learn (instant feedback)' },
              { value: true, label: 'Mock Event (timed, no reveal)' },
            ]}
            value={eventMode}
            onChange={setEventMode}
            disabled={processing}
          />
          {eventMode && countdownSeconds === 0 && (
            <div className="op-field-hint">
              Mock Event is timed — a 5-minute clock will be used.
            </div>
          )}
        </div>

        <div className="op-field">
          <div className="op-field-label">
            <Icon name="clock outline" fitted /> Timer
          </div>
          <ChipGroup
            options={TIMER_OPTIONS}
            value={countdownSeconds}
            onChange={setCountdownSeconds}
            disabled={processing}
          />
        </div>

        <button
          type="button"
          className="op-play"
          onClick={fetchData}
          disabled={processing}
        >
          <Icon
            name={processing ? 'spinner' : eventMode ? 'flag checkered' : 'play'}
            loading={processing}
            fitted
          />
          {processing
            ? 'Loading…'
            : eventMode
            ? 'Start Mock Event'
            : 'Play Now'}
        </button>

        {missed > 0 && (
          <button
            type="button"
            className="op-review"
            onClick={reviewMissed}
            disabled={processing}
          >
            <Icon name="redo" fitted /> Review Missed ({missed})
          </button>
        )}
      </div>
    </div>
  );
};

Main.propTypes = {
  startQuiz: PropTypes.func.isRequired,
};

export default Main;
