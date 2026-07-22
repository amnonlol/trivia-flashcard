import React, { useState, useEffect } from 'react';
import PropTypes from 'prop-types';
import { Icon } from 'semantic-ui-react';
import Swal from 'sweetalert2';

const Countdown = ({ countdownTime, timeOver, setTimeTaken }) => {
  // No countdown selected → run as a count-up stopwatch so the quiz never
  // times out but "Time Taken" is still tracked for the results screen.
  const isCountUp = countdownTime === 0;
  const totalTime = countdownTime * 1000;
  const [timerTime, setTimerTime] = useState(isCountUp ? 0 : totalTime);

  useEffect(() => {
    const timer = setInterval(() => {
      if (isCountUp) {
        setTimerTime(timerTime + 1000);
        return;
      }

      const newTime = timerTime - 1000;

      if (newTime >= 0) {
        setTimerTime(newTime);
      } else {
        clearInterval(timer);

        Swal.fire({
          icon: 'info',
          title: `Oops! Time's up.`,
          text: 'See how you did!',
          confirmButtonText: 'Check Results',
          timer: 5000,
          willClose: () => timeOver(totalTime - timerTime),
        });
      }
    }, 1000);

    return () => {
      clearInterval(timer);
      setTimeTaken(isCountUp ? timerTime : totalTime - timerTime + 1000);
    };

    // eslint-disable-next-line
  }, [timerTime]);

  // Compact mm:ss pill. Counts up when there's no timer, counts down otherwise
  // and turns red in the final 30 seconds.
  const totalSeconds = Math.max(0, Math.round(timerTime / 1000));
  const mm = Math.floor(totalSeconds / 60);
  const ss = totalSeconds % 60;
  const label = `${mm}:${String(ss).padStart(2, '0')}`;
  const low = !isCountUp && timerTime <= 30000;

  return (
    <span className={`op-timer${low ? ' low' : ''}`}>
      <Icon name={isCountUp ? 'stopwatch' : 'clock outline'} fitted />
      {label}
    </span>
  );
};

Countdown.propTypes = {
  countdownTime: PropTypes.number.isRequired,
  timeOver: PropTypes.func.isRequired,
  setTimeTaken: PropTypes.func.isRequired,
};

export default Countdown;
