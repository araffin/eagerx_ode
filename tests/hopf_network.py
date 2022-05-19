"""
CPG in polar coordinates based on:
Pattern generators with sensory feedback for the control of quadruped
authors: L. Righetti, A. Ijspeert
https://ieeexplore.ieee.org/stamp/stamp.jsp?arnumber=4543306

"""

# Registers PybulletEngine
import argparse
import glob
import os
import time
import uuid
from copy import deepcopy
from typing import Dict, Tuple

import gym
import numpy as np

import eagerx
import eagerx_pybullet  # noqa: F401
import numpy as np
import pybullet
import yaml
from eagerx.wrappers import Flatten
from sb3_contrib import TQC
from stable_baselines3.common.callbacks import CheckpointCallback
from stable_baselines3.common.evaluation import evaluate_policy

import eagerx_quadruped.cartesian_control  # noqa: F401
import eagerx_quadruped.cpg_gait  # noqa: F401

# Registers PybulletEngine
import eagerx_quadruped.object  # noqa: F401
import eagerx_quadruped.robots.go1.configs_go1 as go1_config  # noqa: F401

# from stable_baselines3.common.env_checker import check_env


# TODO: Use scipy to accurately integrate cpg
# todo: Specify realistic spaces for cpg.inputs.offset, cpg.outputs.xs_zs
# todo: Specify realistic spaces for quadruped.sensors.{base_orientation, base_velocity, base_position, vel, pos, force_torque}
# todo: Reduce dimension of force_torque sensor (gives [Fx, Fy, Fz, Mx, My, Mz] PER joint --> 6 * 12=72 dimensions).
# todo: Tune sensor rates to the lowest possible.


def get_latest_run_id(log_path: str, env_id: str) -> int:
    """
    Returns the latest run number for the given log name and log path,
    by finding the greatest number in the directories.

    :param log_path: path to log folder
    :param env_id:
    :return: latest run number
    """
    max_run_id = 0
    for path in glob.glob(os.path.join(log_path, env_id + "_[0-9]*")):
        file_name = os.path.basename(path)
        ext = file_name.split("_")[-1]
        if env_id == "_".join(file_name.split("_")[:-1]) and ext.isdigit() and int(ext) > max_run_id:
            max_run_id = int(ext)
    return max_run_id


class StoreDict(argparse.Action):
    """
    Custom argparse action for storing dict.

    In: args1:0.0 args2:"dict(a=1)"
    Out: {'args1': 0.0, arg2: dict(a=1)}
    """

    def __init__(self, option_strings, dest, nargs=None, **kwargs):
        self._nargs = nargs
        super().__init__(option_strings, dest, nargs=nargs, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        arg_dict = {}
        for arguments in values:
            key = arguments.split(":")[0]
            value = ":".join(arguments.split(":")[1:])
            # Evaluate the string as python code
            arg_dict[key] = eval(value)
        setattr(namespace, self.dest, arg_dict)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-f", "--folder", help="Log folder", type=str, default="logs")
    parser.add_argument(
        "-l",
        "--load-checkpoint",
        type=int,
        help="Load checkpoint instead of last model if available, "
        "you must pass the number of timesteps corresponding to it",
    )
    # parser.add_argument(
    #     "--load-last-checkpoint",
    #     action="store_true",
    #     default=False,
    #     help="Load last checkpoint instead of last model if available",
    # )
    parser.add_argument("-t", "--timeout", help="Episode timeout in second", type=int, default=10)
    parser.add_argument("-v", "--desired-vel", help="Desired angular velocity (yaw vel)", type=float, default=20)
    parser.add_argument("--render", action="store_true", default=False, help="Show GUI")
    parser.add_argument("--debug", action="store_true", default=False, help="Show debug")
    parser.add_argument(
        "-params",
        "--hyperparams",
        type=str,
        nargs="+",
        action=StoreDict,
        help="Overwrite hyperparameter (e.g. learning_rate:0.01 train_freq:10)",
    )
    parser.add_argument(
        "--track",
        action="store_true",
        default=False,
        help="if toggled, this experiment will be tracked with Weights and Biases",
    )

    args = parser.parse_args()

    episode_timeout = args.timeout  # in s
    env_rate = 50
    cpg_rate = 200
    cartesian_rate = 200
    quad_rate = 200
    sim_rate = 200
    # sensors = ["pos", "vel", "base_orientation", "base_pos", "base_vel", "force_torque"]  # Todo: works
    # sensors = ["pos", "vel", "base_orientation", "base_pos", "base_vel"]
    sensors = ["base_orientation", "force_torque"]
    # set_random_seed(1)

    env_id = "Quadruped"
    desired_velocity = args.desired_vel
    print(f"Desired angular velocity: {desired_velocity} deg/s")

    roscore = eagerx.initialize("eagerx_core", anonymous=True, log_level=eagerx.log.INFO)

    # Initialize empty graph
    graph = eagerx.Graph.create()

    # Create robot
    robot = eagerx.Object.make(
        "Quadruped",
        "quadruped",
        actuators=["joint_control"],
        sensors=sensors,
        rate=quad_rate,
        control_mode="position_control",
        self_collision=False,
        fixed_base=False,
    )
    # TODO: tune sensor rates to the lowest possible.
    robot.sensors.pos.rate = env_rate
    robot.sensors.vel.rate = env_rate
    robot.sensors.force_torque.rate = env_rate
    robot.sensors.base_orientation.rate = env_rate
    robot.sensors.base_pos.rate = env_rate
    robot.sensors.base_vel.rate = env_rate
    graph.add(robot)

    # Create cartesian control node
    cartesian_control = eagerx.Node.make("CartesiandPDController", "cartesian_control", rate=cartesian_rate)
    graph.add(cartesian_control)

    # Create cpg node
    gait = "TROT"
    omega_swing, omega_stance = {
        "JUMP": [4 * np.pi, 40 * np.pi],
        "TROT": [16 * np.pi, 4 * np.pi],
        "WALK": [16 * np.pi, 4 * np.pi],
        "PACE": [20 * np.pi, 20 * np.pi],
        "BOUND": [10 * np.pi, 20 * np.pi],
    }[gait]
    cpg = eagerx.Node.make(
        "CpgGait",
        "cpg",
        rate=cpg_rate,
        gait=gait,
        omega_swing=omega_swing,
        omega_stance=omega_stance,
    )
    graph.add(cpg)

    # Connect the nodes
    # Note: base angular velocity missing
    graph.connect(action="offset", target=cpg.inputs.offset)
    graph.connect(source=cpg.outputs.cartesian_pos, target=cartesian_control.inputs.cartesian_pos)
    graph.connect(source=cartesian_control.outputs.joint_pos, target=robot.actuators.joint_control)
    if "position" in sensors:
        graph.connect(observation="position", source=robot.sensors.pos)
    if "velocity" in sensors:
        graph.connect(observation="velocity", source=robot.sensors.vel)
    if "position" in sensors:
        graph.connect(observation="base_pos", source=robot.sensors.base_pos)
    if "force_torque" in sensors:
        graph.connect(observation="force_torque", source=robot.sensors.force_torque)
    if "base_vel" in sensors:
        graph.connect(observation="base_vel", source=robot.sensors.base_vel)
    assert "base_orientation" in sensors, (
        "The base_orientation must always be included in the sensors, because it is used to calculate the reward."
    )
    graph.connect(observation="base_orientation", source=robot.sensors.base_orientation, window=2)  # window=2
    if "xs_zs" in sensors:
        graph.connect(
            observation="xs_zs",
            source=cpg.outputs.xs_zs,
            skip=True,
            initial_obs=[-0.01354526, -0.26941818, 0.0552178, -0.25434446],
        )

    # Show in the gui
    # graph.gui()

    show_gui = args.render or args.load_checkpoint is not None

    # Define engine
    engine = eagerx.Engine.make(
        "PybulletEngine",
        rate=sim_rate,
        gui=show_gui,
        egl=True,
        sync=True,
        real_time_factor=0,
        process=eagerx.process.NEW_PROCESS,
    )


    class QuadrupedEnv(eagerx.BaseEnv):
        def __init__(self, name, rate, graph, engine, episode_timeout, force_start=True, debug=False):
            super().__init__(name, rate, graph, engine, force_start=force_start)
            self.steps = None
            self.debug = debug
            self.timeout_steps = int(episode_timeout * rate)

        @property
        def observation_space(self) -> gym.spaces.Dict:
            return self._observation_space

        @property
        def action_space(self) -> gym.spaces.Dict:
            return self._action_space

        def reset(self):
            # Reset number of steps
            self.steps = 0

            # Sample desired states
            states = self.state_space.sample()

            # Perform reset
            obs = self._reset(states)
            return obs

        def step(self, action: Dict) -> Tuple[Dict, float, bool, Dict]:
            obs = self._step(action)
            self.steps += 1

            # Calculate reward
            alive_bonus = 0.25

            # Convert Quaternion to Euler
            _, _, prev_yaw = pybullet.getEulerFromQuaternion(obs["base_orientation"][-2])
            roll, pitch, yaw = pybullet.getEulerFromQuaternion(obs["base_orientation"][-1])

            # Current angular velocity
            yaw_rate = (yaw - prev_yaw) * env_rate
            desired_yaw_rate = np.deg2rad(desired_velocity)

            # yaw_cost = np.linalg.norm(yaw_rate - desired_yaw_rate)
            yaw_cost = (yaw_rate - desired_yaw_rate) ** 2
            reward = alive_bonus - yaw_cost

            if self.debug:
                # print(len(obs["base_vel"][0]), len(obs["velocity"][0]))
                print(yaw_cost)
                # print(obs["base_vel"][0][:2])

            # print(list(map(np.rad2deg, (roll, pitch, yaw))))
            has_fallen = abs(np.rad2deg(roll)) > 40 or abs(np.rad2deg(pitch)) > 40
            timeout = self.steps >= self.timeout_steps

            # Determine done flag
            done = timeout or has_fallen
            # Set info about episode truncation
            info = {"TimeLimit.truncated": timeout and not has_fallen}
            return obs, reward, done, info

    # Initialize Environment
    # Unique ID to be able to launch multiple instances
    env = QuadrupedEnv(
        name=f"Quadruped{uuid.uuid4()}".replace("-", "_"),
        rate=env_rate,
        graph=graph,
        engine=engine,
        episode_timeout=episode_timeout,
        debug=args.debug,
    )
    env = Flatten(env)

    if args.load_checkpoint is not None:
        print(f"Loading {args.folder}/rl_model_{args.load_checkpoint}_steps.zip")
        model = TQC.load(f"{args.folder}/rl_model_{args.load_checkpoint}_steps.zip")
        mean_reward, std = evaluate_policy(model, env, n_eval_episodes=5)
        print(f"Mean reward = {mean_reward:.2f} +/- {std}")
        exit()

    # Default values
    hyperparams = dict(
        learning_rate=1e-3,
        tau=0.02,
        gamma=0.98,
        buffer_size=300000,
        learning_starts=100,
        use_sde=True,
        use_sde_at_warmup=True,
        train_freq=8,
        gradient_steps=10,
        verbose=1,
        top_quantiles_to_drop_per_net=0,
        policy_kwargs=dict(n_critics=1, net_arch=dict(pi=[64, 64], qf=[64, 64])),
    )
    if args.hyperparams is not None:
        hyperparams.update(args.hyperparams)

    config = deepcopy(vars(args))
    config.update(hyperparams)
    config.update(dict(desired_velocity=desired_velocity))

    if args.track:
        try:
            import wandb
        except ImportError:
            raise ImportError(
                "if you want to use Weights & Biases to track experiment, please install W&B via `pip install wandb`"
            )

        run_name = f"{env_id}__TQC__{int(time.time())}"
        run = wandb.init(
            name=run_name,
            project="eagerx",
            entity="sb3",
            config=config,
            sync_tensorboard=True,  # auto-upload sb3's tensorboard metrics
            monitor_gym=True,  # auto-upload the videos of agents playing the game
            save_code=True,  # optional
        )
        hyperparams["tensorboard_log"] = f"runs/{run_name}"

    # env = check_env(env)
    model = TQC("MlpPolicy", env, **hyperparams)
    # Save env config inside model
    model.desired_velocity = desired_velocity

    log_path = args.folder

    exp_id = get_latest_run_id(log_path, env_id) + 1
    log_path = os.path.join(log_path, f"{env_id}_{exp_id}")
    os.makedirs(log_path, exist_ok=True)

    print(f"Saving to {log_path}")

    # save hyperparams
    with open(f"{log_path}/config.yml", "w") as f:
        yaml.dump(config, f)

    # Save a checkpoint every 10000 steps
    checkpoint_callback = CheckpointCallback(save_freq=5000, save_path=log_path, name_prefix="rl_model")

    try:
        model.learn(1_000_000, callback=checkpoint_callback)
    except KeyboardInterrupt:
        model.save("tqc_cpg")

    print("Shutting down")
    env.shutdown()
    if roscore:
        roscore.shutdown()
    print("Shutdown")
