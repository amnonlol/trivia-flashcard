import React, { useState, useEffect, useRef } from 'react';
import PropTypes from 'prop-types';
import { Icon } from 'semantic-ui-react';
import he from 'he';

import Countdown from '../Countdown';
import { getLetter, questionId } from '../../utils';

// A distinct, always-last choice that lets the user admit they don't know
// instead of guessing. It never counts as correct — its purpose is to turn the
// question into a teaching moment by immediately revealing the answer.
const I_DONT_KNOW = "I don't know";

// How long the correct/wrong answer flashes before the quiz auto-advances.
const FLASH_DURATION = 1400;

const Quiz = ({ data, countdownTime, eventMode, endQuiz }) => {
  const [questionIndex, setQuestionIndex] = useState(0);
  // The user's answer per question, keyed by index, so navigating back and
  // forth preserves what was chosen and never double-counts a point.
  const [answers, setAnswers] = useState({});
  // True only during the brief flash right after answering, while the quiz is
  // about to auto-advance. Reviewing a past question does not flash.
  const [flashing, setFlashing] = useState(false);
  const [timeTaken, setTimeTaken] = useState(null);
  const advanceTimer = useRef(null);

  useEffect(() => {
    if (questionIndex > 0) window.scrollTo({ top: 0, behavior: 'smooth' });
  }, [questionIndex]);

  // Clear any pending auto-advance if the component unmounts.
  useEffect(() => () => clearTimeout(advanceTimer.current), []);

  const currentQuestion = data[questionIndex];
  const correctAnswer = he.decode(currentQuestion.correct_answer);
  const selectedAnswer = answers[questionIndex] ?? null;
  const isRevealed = selectedAnswer !== null;
  // Mock-Event mode never reveals correctness mid-quiz — the score lands only at
  // the end. `reveal` gates every piece of answer-feedback UI; when it's false
  // the answer stays changeable, like a real event answer sheet.
  const reveal = isRevealed && !eventMode;
  const isLastQuestion = questionIndex === data.length - 1;
  const gotItRight = selectedAnswer === correctAnswer;
  const saidDontKnow = selectedAnswer === I_DONT_KNOW;

  const scoreOf = allAnswers =>
    data.reduce(
      (total, q, idx) =>
        allAnswers[idx] === he.decode(q.correct_answer) ? total + 1 : total,
      0
    );

  const buildQnA = allAnswers =>
    data.map((q, idx) => {
      const userAnswer = allAnswers[idx] ?? 'No answer';
      const answer = he.decode(q.correct_answer);
      return {
        // Identity + metadata used to record per-profile progress. The Result
        // view ignores these extra keys and only reads the display fields.
        id: questionId(q.question),
        category: q.category,
        difficulty: q.difficulty,
        question: he.decode(q.question),
        user_answer: userAnswer,
        correct_answer: answer,
        source: q.source,
        point: userAnswer === answer ? 1 : 0,
      };
    });

  const finish = allAnswers =>
    endQuiz({
      totalQuestions: data.length,
      correctAnswers: scoreOf(allAnswers),
      timeTaken,
      questionsAndAnswers: buildQnA(allAnswers),
    });

  // Picking an answer flashes the result. A *correct* answer then auto-advances
  // to the next question (or finishes on the last one) — no need to dwell. A
  // *wrong* answer or "I don't know" instead stops on an explainer so the player
  // can read who/what the question was about and the right answer, then taps
  // Continue to move on. In event mode there's no reveal or auto-advance: the
  // choice is just recorded (and can be changed) and the player moves on with the
  // Next button.
  const handleSelect = answer => {
    if (reveal) return; // learn mode: locked after the first answer

    const nextAnswers = { ...answers, [questionIndex]: answer };
    setAnswers(nextAnswers);

    if (eventMode) return;

    const isRight = answer === correctAnswer;
    setFlashing(true);
    advanceTimer.current = setTimeout(() => {
      setFlashing(false);
      // Wrong / "I don't know": hold on the explainer (the reveal panel + the
      // Continue button now appear because flashing is off). Correct: advance.
      if (!isRight) return;
      if (isLastQuestion) {
        finish(nextAnswers);
      } else {
        setQuestionIndex(idx => idx + 1);
      }
    }, FLASH_DURATION);
  };

  const goToPrevious = () => {
    clearTimeout(advanceTimer.current);
    setFlashing(false);
    setQuestionIndex(idx => Math.max(0, idx - 1));
  };

  const goToNext = () => {
    clearTimeout(advanceTimer.current);
    setFlashing(false);
    if (isLastQuestion) {
      finish(answers);
    } else {
      setQuestionIndex(idx => idx + 1);
    }
  };

  const timeOver = timeTaken => {
    clearTimeout(advanceTimer.current);
    return endQuiz({
      totalQuestions: data.length,
      correctAnswers: scoreOf(answers),
      timeTaken,
      questionsAndAnswers: buildQnA(answers),
    });
  };

  // Class that drives the flash animation / static reveal tint per option. In
  // event mode the picked option gets a neutral highlight — no green/red, since
  // correctness stays hidden until the results screen.
  const optionClass = option => {
    if (eventMode) return option === selectedAnswer ? 'op-selected' : undefined;
    if (!isRevealed) return undefined;
    if (option === correctAnswer) {
      return flashing ? 'op-flash-green' : 'op-reveal-correct';
    }
    if (option === selectedAnswer) {
      return flashing ? 'op-flash-red' : 'op-reveal-wrong';
    }
    return undefined;
  };

  const progress = ((questionIndex + 1) / data.length) * 100;

  return (
    <div className="op-shell">
      <div className="op-card">
        <div className="op-quiz-top">
          <span className="op-quiz-count">
            Question {questionIndex + 1} of {data.length}
          </span>
          <Countdown
            countdownTime={countdownTime}
            timeOver={timeOver}
            setTimeTaken={setTimeTaken}
          />
        </div>
        <div className="op-progress">
          <span style={{ width: `${progress}%` }} />
        </div>

        <h2 className="op-question">
          {he.decode(currentQuestion.question)}
        </h2>

        <div className="op-options">
          {currentQuestion.options.map((option, i) => {
            const letter = getLetter(i);
            const decodedOption = he.decode(option);
            const showCorrect = reveal && decodedOption === correctAnswer;
            const showWrong =
              reveal && decodedOption === selectedAnswer && !gotItRight;
            const dimmed = reveal && !showCorrect && !showWrong;

            return (
              <button
                key={decodedOption}
                type="button"
                disabled={reveal}
                className={`op-option${dimmed ? ' op-dimmed' : ''}${
                  optionClass(decodedOption)
                    ? ` ${optionClass(decodedOption)}`
                    : ''
                }`}
                onClick={() => handleSelect(decodedOption)}
              >
                <span className="op-letter">{letter}</span>
                {decodedOption}
                {showCorrect && (
                  <Icon name="check circle" color="green" className="op-mark" />
                )}
                {showWrong && (
                  <Icon name="times circle" color="red" className="op-mark" />
                )}
              </button>
            );
          })}
          {/* Fifth, always-last choice — kept out of the shuffled options so
              it stays predictable and never plausible. */}
          <button
            key={I_DONT_KNOW}
            type="button"
            disabled={reveal}
            className={`op-option op-dontknow${
              eventMode && saidDontKnow ? ' op-selected' : ''
            }${reveal && !saidDontKnow ? ' op-dimmed' : ''}${
              reveal && saidDontKnow ? ' idk' : ''
            }`}
            onClick={() => handleSelect(I_DONT_KNOW)}
          >
            <Icon name="question circle outline" fitted /> {I_DONT_KNOW}
          </button>
        </div>

        {reveal && (
          <div
            className={`op-feedback ${
              gotItRight ? 'correct' : saidDontKnow ? 'idk' : 'wrong'
            }`}
          >
            <Icon
              className="op-fb-icon"
              name={
                gotItRight
                  ? 'check circle'
                  : saidDontKnow
                  ? 'lightbulb outline'
                  : 'times circle'
              }
            />
            <div>
              <strong>
                {gotItRight
                  ? 'Correct!'
                  : saidDontKnow
                  ? "No worries — here's the answer:"
                  : 'Not quite.'}
              </strong>
              {!gotItRight && (
                <span>
                  The correct answer is <b>{correctAnswer}</b>.
                </span>
              )}
              {/* Explainer: only on a wrong / "I don't know" answer, turning the
                  miss into a teaching moment. An image shows alongside when the
                  question carries one; a broken/absent image simply hides. */}
              {!gotItRight && currentQuestion.explainer && (
                <div className="op-explainer">
                  {currentQuestion.image && (
                    <img
                      className="op-explainer-img"
                      src={currentQuestion.image}
                      alt=""
                      loading="lazy"
                      // Fandom's CDN hotlink-protects: a request carrying our
                      // github.io Referer 404s, but a referer-less one is served.
                      // Omit the Referer so portraits actually load off-wiki.
                      referrerPolicy="no-referrer"
                      onError={e => {
                        e.currentTarget.style.display = 'none';
                      }}
                    />
                  )}
                  <p>{currentQuestion.explainer}</p>
                </div>
              )}
              {currentQuestion.source && (
                <div style={{ marginTop: 6 }}>
                  <a
                    href={currentQuestion.source}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    <Icon name="external" /> Learn more on the One Piece Wiki
                  </a>
                </div>
              )}
            </div>
          </div>
        )}

        <div className="op-nav">
          {/* Back button to revisit and review the previous question after
              the quiz has auto-advanced. */}
          <button
            type="button"
            className="op-nav-btn"
            onClick={goToPrevious}
            disabled={questionIndex === 0}
          >
            <Icon name="arrow left" fitted /> Previous
          </button>
          {/* Learn mode shows the advance button after the reveal; event mode
              always shows it so the player can advance (or skip) at their own
              pace. On a wrong / "I don't know" answer it reads "Continue" —
              the player is dismissing the explainer, not skipping ahead. */}
          {(eventMode || (reveal && !flashing)) && (
            <button
              type="button"
              className="op-nav-btn primary"
              onClick={goToNext}
            >
              {isLastQuestion
                ? 'Finish Quiz'
                : !eventMode && !gotItRight
                ? 'Continue'
                : 'Next'}{' '}
              <Icon
                name={isLastQuestion ? 'flag checkered' : 'arrow right'}
                fitted
              />
            </button>
          )}
        </div>
      </div>
    </div>
  );
};

Quiz.propTypes = {
  data: PropTypes.array.isRequired,
  countdownTime: PropTypes.number.isRequired,
  eventMode: PropTypes.bool,
  endQuiz: PropTypes.func.isRequired,
};

export default Quiz;
