// React context wrapper around utils/profiles.js so any component can read the
// active profile and re-render when it changes, without prop-drilling through
// the App/Main/Header tree. The store (localStorage) stays the source of truth;
// this just mirrors it into React state and re-syncs on mutations.
import React, { createContext, useContext, useState, useCallback } from 'react';
import PropTypes from 'prop-types';

import {
  listProfiles,
  getActiveProfile,
  setActiveProfile,
  createProfile as createProfileInStore,
  deleteProfile as deleteProfileInStore,
} from '../utils/profiles';

const ProfileContext = createContext(null);

export const ProfileProvider = ({ children }) => {
  const [profiles, setProfiles] = useState(() => listProfiles());
  const [activeProfile, setActiveProfileState] = useState(() =>
    getActiveProfile()
  );

  // Pull the latest profiles + active selection from the store into state.
  const refresh = useCallback(() => {
    setProfiles(listProfiles());
    setActiveProfileState(getActiveProfile());
  }, []);

  const switchProfile = useCallback(
    id => {
      setActiveProfile(id);
      refresh();
    },
    [refresh]
  );

  const createProfile = useCallback(
    name => {
      const profile = createProfileInStore(name);
      refresh();
      return profile;
    },
    [refresh]
  );

  const deleteProfile = useCallback(
    id => {
      deleteProfileInStore(id);
      refresh();
    },
    [refresh]
  );

  const value = {
    profiles,
    activeProfile,
    switchProfile,
    createProfile,
    deleteProfile,
    refresh,
  };

  return (
    <ProfileContext.Provider value={value}>
      {children}
    </ProfileContext.Provider>
  );
};

ProfileProvider.propTypes = {
  children: PropTypes.node,
};

export const useProfile = () => {
  const context = useContext(ProfileContext);
  if (!context) {
    throw new Error('useProfile must be used within a ProfileProvider');
  }
  return context;
};

export default ProfileContext;
