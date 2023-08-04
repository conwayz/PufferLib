from pdb import set_trace as T

import numpy as np
import warnings

import gym
from collections import OrderedDict, Mapping

import pufferlib
from pufferlib import utils, exceptions


class Postprocessor:
    def __init__(self, env):
        self.env = env

    def reset(self, team_obs):
        self.epoch_return = 0
        self.epoch_length = 0
        self.done = False

    def features(self, obs):
        '''Default featurizer pads observations to max team size'''
        return obs

    def actions(self, actions):
        return actions

    def rewards_dones_infos(self, rewards, dones, infos):
        return rewards, dones, infos
        rewards = sum(rewards.values())

        if env_done:
            team_infos['return'] = self.epoch_return
            team_infos['length'] = self.epoch_length
            self.done = True
        elif not team_done:
            self.epoch_length += 1
            self.epoch_return += team_reward

        return team_infos


class GymPufferEnv:
    def __init__(self, env=None, env_cls=None, env_args=[], env_kwargs={}, postprocessor_cls=Postprocessor):
        self.env = make_env(env, env_cls, env_args, env_kwargs)
        self.postprocessor = postprocessor_cls(self.env)
        self.initialized = False
        self.done = True

        # Cache the observation and action spaces
        self.observation_space
        self.action_space

    @property
    def observation_space(self):
        '''Returns a flattened, single-tensor observation space'''
        # Call user featurizer and create a corresponding gym space
        featurized_ob_space, featurized_ob = pufferlib.new_emulation.make_featurized_obs_and_space(self.env.observation_space, self.postprocessor)

        # Flatten the featurized observation space and store it for use in step. Return a box space for the user
        self.flat_ob_space, self.box_ob_space, self.pad_ob = pufferlib.new_emulation.make_flat_and_box_obs_space(featurized_ob_space, featurized_ob)

        return self.box_ob_space

    @property
    def action_space(self):
        '''Returns a flattened, multi-discrete action space'''
        # Store a flat version of the action space for use in step. Return a multidiscrete version for the user
        self.flat_action_space, multi_discrete_action_space = pufferlib.new_emulation.make_flat_and_multidiscrete_atn_space(self.env.action_space)
        return multi_discrete_action_space

    def reset(self):
        ob = self.env.reset()
        self.initialized = True
        self.done = False
        # Call user featurizer and flatten the observations
        return pufferlib.new_emulation.postprocess_and_flatten(
            ob, self.postprocessor, self.flat_ob_space, reset=True)

    def step(self, action):
        '''Execute an action and return (observation, reward, done, info)'''
        if not self.initialized:
            raise exceptions.APIUsageError('step() called before reset()')
        if self.done:
            raise exceptions.APIUsageError('step() called after environment is done')

        processed_action = self.postprocessor.actions(action)

        if not self.action_space.contains(processed_action):
            raise ValueError(
                f'Action:\n{processed_action}\n not in space:\n{self.flat_action_space}')

        # Unpack actions from multidiscrete into the original action space
        action = unpack_actions(action, self.flat_action_space)

        ob, reward, done, info = self.env.step(processed_action)
        self.done = done

        # Call user postprocessors and flatten the observations
        processed_ob, single_reward, single_done, single_info = postprocess_and_flatten(
            ob, self.postprocessor, self.flat_ob_space, reward, done, info)

        if not self.observation_space.contains(processed_ob):
            raise ValueError(
                f'Observation:\n{processed_ob}\n not in space:\n{self.observation_space}')

        return processed_ob, single_reward, single_done, single_info


class PettingZooPufferEnv:
    def __init__(self, env=None, env_cls=None, env_args=[], env_kwargs={}, postprocessor_cls=Postprocessor, teams=None, max_horizon=None):
        self.env = make_env(env, env_cls, env_args, env_kwargs)
        self.initialized = False
        self.done = True

        self.possible_agents = self.env.possible_agents if teams is None else list(teams.keys())
        self.teams = teams

        self.postprocessors = {agent: postprocessor_cls(self.env)
            for agent in self.possible_agents}

        # TODO: Handle caching
        agent = self.possible_agents[0]
        self.observation_space(agent)
        self.action_space(agent)

    def observation_space(self, agent):
        '''Returns the observation space for a single agent'''
        if agent not in self.possible_agents:
            raise pufferlib.exceptions.InvalidAgentError(agent, self.possible_agents)

        # Make a gym space defining observations for the whole team
        if self.teams is not None:
            obs_space = pufferlib.new_emulation.make_team_space(
                self.env.observation_space, self.teams[agent])
        else:
            obs_space = self.env.observation_space(agent)

        # Call user featurizer and create a corresponding gym space
        featurized_obs_space, featurized_obs = make_featurized_obs_and_space(obs_space, self.postprocessors[agent])

        # Flatten the featurized observation space and store it for use in step. Return a box space for the user
        self.flat_obs_space, self.box_obs_space, self.pad_obs = make_flat_and_box_obs_space(featurized_obs_space, featurized_obs)

        return self.box_obs_space

    def action_space(self, agent):
        '''Returns the action space for a single agent'''
        if agent not in self.possible_agents:
            raise pufferlib.exceptions.InvalidAgentError(agent, self.possible_agents)

        # Make a gym space defining actions for the whole team
        if self.teams is not None:
            atn_space = pufferlib.new_emulation.make_team_space(
                self.env.action_space, self.teams[agent])
        else:
            atn_space = self.env.action_space(agent)

        # Store a flat version of the action space for use in step. Return a multidiscrete version for the user
        self.flat_action_space, multidiscrete_action_space = pufferlib.new_emulation.make_flat_and_multidiscrete_atn_space(atn_space)

        return multidiscrete_action_space

    def reset(self):
        obs = self.env.reset()
        self.initialized = True
        self.done = False

        # Group observations into teams
        if self.teams is not None:
            obs = pufferlib.new_emulation.group_into_teams(self.teams, obs)

        # Call user featurizer and flatten the observations
        postprocessed_obs = {}
        for agent in self.possible_agents:
            postprocessed_obs[agent] = postprocess_and_flatten(
                obs[agent], self.postprocessors[agent], self.flat_obs_space, reset=True)
            
        self.agents = list(postprocessed_obs)
        return postprocessed_obs

    def step(self, actions):
        '''Step the environment and return (observations, rewards, dones, infos)'''
        if not self.initialized:
            raise exceptions.APIUsageError('step() called before reset()')
        if self.done:
            raise exceptions.APIUsageError('step() called after environment is done')
        if __debug__:
            for agent, atn in actions.items():
                if agent not in self.agents:
                    raise exceptions.InvalidAgentError(agent, self.agents)

        # Postprocess actions and validate action spaces
        for agent in actions:
            actions[agent] = self.postprocessors[agent].actions(actions[agent])

        pufferlib.new_emulation.check_spaces(actions, self.action_space)

        # Unpack actions from multidiscrete into the original action space
        '''
        split_actions = {}
        for team_id, team in agent_ids.items():
            # TODO: Assert all keys present since actions are padded
            team_atns = np.split(actions[team_id], len(team))
            for agent_id, atns in zip(team, team_atns):
                split_actions[agent_id] = atns

        if k not in agent_ids:
            del(actions[k])
            continue
        '''
        unpacked_actions = {}
        for agent, atn in actions.items():
            unpacked_actions[agent] = unpack_actions(atn, self.flat_action_space)

        # Ungroup actions from teams, step the env, and group the env outputs
        if self.teams is not None:
            team_obs, rewards, dones, infos = team_ungroup_step_group(
                self, self.teams, self.env, unpack_actions)
        else:
            team_obs, rewards, dones, infos = self.env.step(unpack_actions)

        # Call user postprocessors and flatten the observations
        featurized_obs = {}
        for agent in self.possible_agents:
            featurized_obs[agent], rewards[agent], dones[agent], infos[agent] = postprocess_and_flatten(
                team_obs[agent], self.postprocessors[agent], self.flat_obs_space,
                rewards[agent], dones[agent], infos[agent])

        self.agents = list(featurized_obs)
        postprocessed_obs, reward, done, info = pufferlib.new_emulation.pad_to_const_num_agents(
            self.possible_agents, featurized_obs, rewards, dones, infos, self.pad_obs)

        pufferlib.new_emulation.check_spaces(postprocessed_obs, self.observation_space)
        return postprocessed_obs, rewards, dones, infos
 
def make_env(env, env_cls, env_args=[], env_kwargs={}):
    # TODO: Check env is obs and env_cls is class
    assert bool(env) != bool(env_cls), 'Must provide either env or env_cls, but not both'
    if env_cls:
        return env_cls(*env_args, **env_kwargs)
    return env

def team_ungroup_step_group(self, teams, env, actions):
    actions = pufferlib.new_emulation.ungroup_from_teams(actions)
    obs, rewards, dones, infos = env.step(actions)
    team_obs, rewards, dones = pufferlib.new_emulation.group_into_teams(
            teams, obs, rewards, dones)
    return team_obs, rewards, dones, infos


def pad_to_const_num_agents(teams, obs, rewards, dones, infos, pad_obs):
    padded_obs = pad_agent_data(obs, teams, pad_obs)
    rewards = pad_agent_data(rewards, teams, 0)
    dones = pad_agent_data(dones, teams, False)
    infos = pad_agent_data(infos, teams, {})
    return padded_obs, rewards, dones, infos


def postprocess_and_flatten(ob, postprocessor, flat_obs_space,
        reward=None, done=None, info=None,
        reset=False, max_horizon=None):
    if reset:
        postprocessor.reset(ob)
    else:
        reward, done, info = postprocessor.rewards_dones_infos(
            reward, done, info)

    postprocessed_ob = postprocessor.features(ob)
    flat_ob = pufferlib.new_emulation.flatten_to_array(ob, flat_obs_space)

    return postprocessed_ob, reward, done, info


def make_flat_and_multidiscrete_atn_space(atn_space):
    flat_action_space = pufferlib.new_emulation.flatten_space(atn_space)
    multidiscrete_space = pufferlib.new_emulation.convert_to_multidiscrete(flat_action_space)
    return flat_action_space, multidiscrete_space


def make_flat_and_box_obs_space(obs_space, obs):
    flat_obs_space = pufferlib.new_emulation.flatten_space(obs_space)  
    flat_obs = pufferlib.new_emulation.flatten_to_array(obs, flat_obs_space)

    mmin, mmax = pufferlib.utils._get_dtype_bounds(flat_obs.dtype)
    pad_obs = 0 * flat_obs
    box_obs_space = gym.spaces.Box(
        low=mmin, high=mmax,
        shape=flat_obs.shape, dtype=flat_obs.dtype
    )

    return flat_obs_space, box_obs_space, pad_obs


def make_featurized_obs_and_space(obs_space, postprocessor):
    obs_sample = obs_space.sample()
    featurized_obs = postprocessor.features(obs_sample)
    featurized_obs_space = make_space_like(featurized_obs)
    return featurized_obs_space, featurized_obs

def make_team_space(observation_space, agents):
    return gym.spaces.Dict({agent: observation_space(agent) for agent in agents})

def pad_agent_data(data, agents, pad_value):
    return {agent: data[agent] if agent in data else pad_value
        for agent in agents}


def check_spaces(data, spaces):
    for k, v in data.items():
        if not spaces(k).contains(v):
            raise ValueError(
                f'Data:\n{v}\n for agent/team {k} not in '
                f'space:\n{spaces(k)}')


def check_teams(env, teams):
    if set(env.possible_agents) != {item for team in teams.values() for item in team}:
        raise ValueError(f'Invalid teams: {teams} for possible_agents: {env.possible_agents}')

def group_into_teams(teams, *args):
    grouped_data = []

    for agent_data in args:
        if __debug__ and set(agent_data) != {item for team in teams.values() for item in team}:
            raise ValueError(f'Invalid teams: {teams} for agents: {set(agent_data)}')

        team_data = {}
        for team_id, team in teams.items():
            team_data[team_id] = {}
            for agent_id in team:
                if agent_id in agent_data:
                    team_data[team_id][agent_id] = agent_data[agent_id]

        grouped_data.append(team_data)

    if len(grouped_data) == 1:
        return grouped_data[0]

    return grouped_data

def ungroup_from_teams(team_data):
    agent_data = {}
    for team in team_data.values():
        for agent_id, data in team.items():
            agent_data[agent_id] = data
    return agent_data

def unpack_actions(action, flat_space):
    if not isinstance(flat_space, dict):
        action = action.reshape(flat_space.shape)
    elif () in flat_space:
        action = action[0]
    else:
        nested_data = {}

        for key_list, space in flat_space.items():
            current_dict = nested_data

            for key in key_list[:-1]:
                if key not in current_dict:
                    current_dict[key] = {}
                current_dict = current_dict[key]

            last_key = key_list[-1]

            if space.shape:
                size = np.prod(space.shape)
                current_dict[last_key] = flat[:size].reshape(space.shape)
                flat = flat[size:]
            else:
                current_dict[last_key] = flat[0]
                flat = flat[1:]

        action = nested_data

    return action

def unpack_batched_obs(flat_space, packed_obs):
    if not isinstance(flat_space, dict):
        return packed_obs.reshape(packed_obs.shape[0], *flat_space.shape)

    batch = packed_obs.shape[0]

    if () in flat_space:
        return packed_obs.reshape(batch, *flat_space[()].shape)

    batched_obs = {}
    idx = 0

    for key_list, space in flat_space.items():
        current_dict = batched_obs
        inc = int(np.prod(space.shape))

        for key in key_list[:-1]:
            if key not in current_dict:
                current_dict[key] = {}
            current_dict = current_dict[key]

        last_key = key_list[-1]
        shape = space.shape
        if len(shape) == 0:
            shape = (1,)    

        current_dict[last_key] = packed_obs[:, idx:idx + inc].reshape(batch, *shape)
        idx += inc

    return batched_obs

def convert_to_multidiscrete(flat_space):
    lens = []
    for e in flat_space.values():
        if isinstance(e, gym.spaces.Discrete):
            lens.append(e.n)
        elif isinstance(e, gym.spaces.MultiDiscrete):
            lens += e.nvec.tolist()
        else:
            raise ValueError(f'Invalid action space: {e}')

    return gym.spaces.MultiDiscrete(lens)

def make_space_like(ob):
    if type(ob) == np.ndarray:
        mmin, mmax = utils._get_dtype_bounds(ob.dtype)
        return gym.spaces.Box(
            low=mmin, high=mmax,
            shape=ob.shape, dtype=ob.dtype
        )

    # TODO: Handle Discrete (how to get max?)
    if type(ob) in (tuple, list):
        return gym.spaces.Tuple([make_space_like(v) for v in ob])

    if type(ob) in (dict, OrderedDict):
        return gym.spaces.Dict({k: make_space_like(v) for k, v in ob.items()})

    if type(ob) in (int, float):
        # TODO: Tighten bounds
        return gym.spaces.Box(low=-np.inf, high=np.inf, shape=())

    raise ValueError(f'Invalid type for featurized obs: {type(ob)}')

def flatten_space(space):
    flat_keys = {}

    def _recursion_helper(current_space, key_list):
        if isinstance(current_space, (list, tuple, gym.spaces.Tuple)):
            for idx, elem in enumerate(current_space):
                new_key_list = key_list + (idx,)
                _recursion_helper(elem, new_key_list)
        elif isinstance(current_space, (dict, OrderedDict, gym.spaces.Dict)):
            for key, value in current_space.items():
                new_key_list = key_list + (key,)
                _recursion_helper(value, new_key_list)
        else:
            flat_keys[key_list] = current_space

    _recursion_helper(space, ())
    return flat_keys

def flatten_to_array(space_sample, flat_space, dtype=None):
    # TODO: Find a better way to handle Atari
    if type(space_sample) == gym.wrappers.frame_stack.LazyFrames:
       space_sample = np.array(space_sample)

    if () in flat_space:
        if isinstance(space_sample, np.ndarray):
            return space_sample.reshape(*flat_space[()].shape)
        return np.array([space_sample])

    tensors = []
    for key_list in flat_space:
        value = space_sample
        for key in key_list:
            try:
                value = value[key]
            except:
                T()

        if not isinstance(value, np.ndarray):
            value = np.array([value])

        tensors.append(value.ravel())

    # Preallocate the memory for the concatenated tensor
    if type(tensors) == dict:
        tensors = tensors.values()

    if dtype is None:
        tensors = list(tensors)
        dtype = tensors[0].dtype

    tensor_sizes = [tensor.size for tensor in tensors]
    prealloc = np.empty(sum(tensor_sizes), dtype=dtype)

    # Fill the concatenated tensor with the flattened tensors
    start = 0
    for tensor, size in zip(tensors, tensor_sizes):
        end = start + size
        prealloc[start:end] = tensor.ravel()
        start = end

    return prealloc

def _seed_and_reset(env, seed):
    try:
        env.seed(seed)
        old_seed=True
    except:
        old_seed=False

    if old_seed:
        obs = env.reset()
    else:
        try:
            obs = env.reset(seed=seed)
        except:
            obs= env.reset()
            warnings.warn('WARNING: Environment does not support seeding.', DeprecationWarning)

    return obs