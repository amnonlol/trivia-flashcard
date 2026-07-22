import React, { useState, useEffect } from 'react';
import {
  Menu,
  Button,
  Dropdown,
  Modal,
  Input,
  Icon,
  List,
} from 'semantic-ui-react';

import { useProfile } from '../../context/ProfileContext';

const Header = () => {
  const [promptEvent, setPromptEvent] = useState(null);
  const [appAccepted, setAppAccepted] = useState(false);

  const {
    profiles,
    activeProfile,
    switchProfile,
    createProfile,
    deleteProfile,
  } = useProfile();

  const [createOpen, setCreateOpen] = useState(false);
  const [manageOpen, setManageOpen] = useState(false);
  const [newName, setNewName] = useState('');

  // First run: with no profiles there's nobody to track progress for, so force
  // the create dialog open until the player makes one. It can't be dismissed.
  const mustCreate = profiles.length === 0;
  useEffect(() => {
    if (mustCreate) setCreateOpen(true);
  }, [mustCreate]);

  let isAppInstalled = false;
  if (window.matchMedia('(display-mode: standalone)').matches || appAccepted) {
    isAppInstalled = true;
  }

  window.addEventListener('beforeinstallprompt', e => {
    e.preventDefault();
    setPromptEvent(e);
  });

  const installApp = () => {
    promptEvent.prompt();
    promptEvent.userChoice.then(result => {
      if (result.outcome === 'accepted') {
        setAppAccepted(true);
      }
    });
  };

  const submitCreate = () => {
    if (!newName.trim()) return;
    createProfile(newName);
    setNewName('');
    setCreateOpen(false);
  };

  return (
    <Menu stackable inverted>
      <Menu.Item header>
        <h1>One Piece Trivia</h1>
      </Menu.Item>

      <Menu.Menu position="right">
        {promptEvent && !isAppInstalled && (
          <Menu.Item>
            <Button
              color="teal"
              icon="download"
              labelPosition="left"
              content="Install App"
              onClick={installApp}
            />
          </Menu.Item>
        )}

        {activeProfile && (
          <Dropdown
            item
            trigger={
              <span>
                <Icon
                  name="user circle"
                  style={{ color: activeProfile.color }}
                />
                {activeProfile.name}
              </span>
            }
          >
            <Dropdown.Menu>
              <Dropdown.Header content="Switch player" />
              {profiles.map(profile => (
                <Dropdown.Item
                  key={profile.id}
                  active={profile.id === activeProfile.id}
                  onClick={() => switchProfile(profile.id)}
                >
                  <Icon
                    name="user circle"
                    style={{ color: profile.color }}
                  />
                  {profile.name}
                  {profile.id === activeProfile.id && (
                    <Icon name="check" style={{ float: 'right' }} />
                  )}
                </Dropdown.Item>
              ))}
              <Dropdown.Divider />
              <Dropdown.Item
                icon="plus"
                text="Add player…"
                onClick={() => setCreateOpen(true)}
              />
              <Dropdown.Item
                icon="setting"
                text="Manage players…"
                onClick={() => setManageOpen(true)}
              />
            </Dropdown.Menu>
          </Dropdown>
        )}
      </Menu.Menu>

      {/* Create-player dialog (also the forced first-run prompt). */}
      <Modal
        size="tiny"
        open={createOpen}
        closeOnDimmerClick={!mustCreate}
        onClose={() => !mustCreate && setCreateOpen(false)}
      >
        <Modal.Header>
          {mustCreate ? 'Welcome! Who’s playing?' : 'Add a player'}
        </Modal.Header>
        <Modal.Content>
          <p>
            Your progress is saved on this device under this name — questions
            you master stop coming up, and difficulty can adapt to your skill.
          </p>
          <Input
            fluid
            autoFocus
            placeholder="Enter a name (e.g. Luffy)"
            value={newName}
            maxLength={24}
            onChange={(e, { value }) => setNewName(value)}
            onKeyDown={e => e.key === 'Enter' && submitCreate()}
          />
        </Modal.Content>
        <Modal.Actions>
          {!mustCreate && (
            <Button onClick={() => setCreateOpen(false)}>Cancel</Button>
          )}
          <Button
            primary
            icon="check"
            content="Create"
            disabled={!newName.trim()}
            onClick={submitCreate}
          />
        </Modal.Actions>
      </Modal>

      {/* Manage / delete players. */}
      <Modal
        size="tiny"
        open={manageOpen}
        onClose={() => setManageOpen(false)}
      >
        <Modal.Header>Manage players</Modal.Header>
        <Modal.Content>
          <List divided verticalAlign="middle" size="large">
            {profiles.map(profile => (
              <List.Item key={profile.id}>
                <List.Content floated="right">
                  <Button
                    basic
                    color="red"
                    size="tiny"
                    icon="trash"
                    content="Delete"
                    onClick={() => deleteProfile(profile.id)}
                  />
                </List.Content>
                <List.Content>
                  <Icon
                    name="user circle"
                    style={{ color: profile.color }}
                  />
                  {profile.name}
                </List.Content>
              </List.Item>
            ))}
          </List>
          <p style={{ color: '#999', marginTop: 12 }}>
            Deleting a player permanently removes their saved progress on this
            device.
          </p>
        </Modal.Content>
        <Modal.Actions>
          <Button primary content="Done" onClick={() => setManageOpen(false)} />
        </Modal.Actions>
      </Modal>
    </Menu>
  );
};

export default Header;
