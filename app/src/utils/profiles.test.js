import {
  questionId,
  createProfile,
  listProfiles,
  getActiveProfileId,
  recordAnswers,
  getProgress,
  masteredIds,
  missedQuestionIds,
  getCategoryStats,
  adaptiveMix,
  getSummary,
  MASTERY_STREAK,
} from './profiles';

beforeEach(() => {
  localStorage.clear();
});

describe('questionId', () => {
  it('is stable for the same text', () => {
    expect(questionId('What is Luffy’s Devil Fruit?')).toEqual(
      questionId('What is Luffy’s Devil Fruit?')
    );
  });

  it('differs for different text', () => {
    expect(questionId('Question A')).not.toEqual(questionId('Question B'));
  });
});

describe('profiles CRUD', () => {
  it('creates a profile and makes it active', () => {
    const p = createProfile('Zoro');
    expect(p.name).toEqual('Zoro');
    expect(listProfiles()).toHaveLength(1);
    expect(getActiveProfileId()).toEqual(p.id);
  });
});

describe('mastery (2 in a row)', () => {
  const q = { id: 'q_test', difficulty: 'easy' };

  it('retires a question after two consecutive correct answers', () => {
    const p = createProfile('Nami');

    recordAnswers(p.id, [{ ...q, point: 1 }]);
    expect(masteredIds(p.id).has('q_test')).toBe(false);

    recordAnswers(p.id, [{ ...q, point: 1 }]);
    expect(masteredIds(p.id).has('q_test')).toBe(true);
    expect(getProgress(p.id).questions.q_test.streak).toBe(MASTERY_STREAK);
  });

  it('resets the streak on a wrong answer so a lucky guess alone never masters', () => {
    const p = createProfile('Usopp');

    recordAnswers(p.id, [{ ...q, point: 1 }]);
    recordAnswers(p.id, [{ ...q, point: 0 }]); // miss resets streak
    recordAnswers(p.id, [{ ...q, point: 1 }]);
    expect(masteredIds(p.id).has('q_test')).toBe(false);

    recordAnswers(p.id, [{ ...q, point: 1 }]); // now two in a row
    expect(masteredIds(p.id).has('q_test')).toBe(true);
  });

  it('reports how many questions newly mastered this round', () => {
    const p = createProfile('Sanji');
    recordAnswers(p.id, [{ id: 'a', difficulty: 'easy', point: 1 }]);
    const newly = recordAnswers(p.id, [
      { id: 'a', difficulty: 'easy', point: 1 },
    ]);
    expect(newly).toBe(1);
  });
});

describe('adaptiveMix', () => {
  it('leans easy for a new/low-data profile', () => {
    const p = createProfile('Chopper');
    const mix = adaptiveMix(p.id);
    expect(mix.easy).toBeGreaterThan(mix.hard);
  });

  it('leans hard once accuracy is high', () => {
    const p = createProfile('Robin');
    // Feed 10 correct hard answers → high accuracy.
    for (let i = 0; i < 10; i += 1) {
      recordAnswers(p.id, [{ id: `h${i}`, difficulty: 'hard', point: 1 }]);
    }
    const mix = adaptiveMix(p.id);
    expect(mix.hard).toBeGreaterThan(mix.easy);
  });
});

describe('getSummary', () => {
  it('aggregates answered, accuracy and mastered', () => {
    const p = createProfile('Franky');
    recordAnswers(p.id, [
      { id: 'x', difficulty: 'easy', point: 1 },
      { id: 'y', difficulty: 'medium', point: 0 },
    ]);
    const s = getSummary(p.id);
    expect(s.answered).toBe(2);
    expect(s.accuracy).toBe(50);
    expect(s.mastered).toBe(0);
  });
});

describe('missedQuestionIds', () => {
  it('lists wrong, un-mastered questions weakest (most-missed) first', () => {
    const p = createProfile('Brook');
    // q_a missed twice, q_b missed once → q_a should rank first.
    recordAnswers(p.id, [
      { id: 'q_a', category: 'Characters', difficulty: 'easy', point: 0 },
      { id: 'q_b', category: 'Bounties', difficulty: 'easy', point: 0 },
    ]);
    recordAnswers(p.id, [
      { id: 'q_a', category: 'Characters', difficulty: 'easy', point: 0 },
    ]);
    expect(missedQuestionIds(p.id)).toEqual(['q_a', 'q_b']);
  });

  it('drops a question once it is mastered', () => {
    const p = createProfile('Jinbe');
    recordAnswers(p.id, [
      { id: 'q_c', category: 'Characters', difficulty: 'easy', point: 0 },
    ]);
    expect(missedQuestionIds(p.id)).toContain('q_c');
    // Two in a row masters it → leaves the review pool.
    recordAnswers(p.id, [
      { id: 'q_c', category: 'Characters', difficulty: 'easy', point: 1 },
    ]);
    recordAnswers(p.id, [
      { id: 'q_c', category: 'Characters', difficulty: 'easy', point: 1 },
    ]);
    expect(missedQuestionIds(p.id)).not.toContain('q_c');
  });
});

describe('getCategoryStats', () => {
  it('tallies per-category accuracy, most-answered first', () => {
    const p = createProfile('Nico');
    recordAnswers(p.id, [
      { id: 'a', category: 'Devil Fruits', difficulty: 'easy', point: 1 },
      { id: 'b', category: 'Devil Fruits', difficulty: 'easy', point: 0 },
      { id: 'c', category: 'Bounties', difficulty: 'easy', point: 1 },
    ]);
    const stats = getCategoryStats(p.id);
    expect(stats[0]).toEqual({
      category: 'Devil Fruits',
      correct: 1,
      total: 2,
      accuracy: 50,
    });
    expect(stats.find(s => s.category === 'Bounties').accuracy).toBe(100);
  });
});
