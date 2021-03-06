# coding=utf-8
# Copyright 2018 The TF-Agents Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

r"""Train and Eval SAC."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import time

from absl import flags

import tensorflow as tf

from tf_agents.agents.ddpg import critic_network
from tf_agents.agents.sac import sac_agent
from tf_agents.drivers import dynamic_step_driver
from tf_agents.environments import suite_mujoco
from tf_agents.environments import tf_py_environment
from tf_agents.metrics import metric_utils
from tf_agents.metrics import py_metrics
from tf_agents.metrics import tf_metrics
from tf_agents.metrics import tf_py_metric
from tf_agents.networks import actor_distribution_network
from tf_agents.networks import normal_projection_network
from tf_agents.policies import py_tf_policy
from tf_agents.replay_buffers import tf_uniform_replay_buffer
from tf_agents.utils import common as common_utils

import gin.tf

nest = tf.contrib.framework.nest

flags.DEFINE_string('root_dir', os.getenv('TEST_UNDECLARED_OUTPUTS_DIR'),
                    'Root directory for writing logs/summaries/checkpoints.')
flags.DEFINE_integer('num_iterations', 100000,
                     'Total number train/eval iterations to perform.')
flags.DEFINE_string('env_name', 'HalfCheetah-v1', 'Agent test environment.')
flags.DEFINE_multi_string('config_file', None,
                          'Path to the trainer config files.')
flags.DEFINE_multi_string('binding', None, 'Gin binding to pass through.')

FLAGS = flags.FLAGS


def _normal_projection_net(action_spec,
                           init_action_stddev=0.35,
                           init_means_output_factor=0.1):
  del init_action_stddev
  return normal_projection_network.NormalProjectionNetwork(
      action_spec,
      mean_transform=None,
      state_dependent_std=True,
      init_means_output_factor=init_means_output_factor,
      std_transform=sac_agent.std_clip_transform)


def train_eval(
    root_dir,
    env_name='HalfCheetah-v1',
    num_iterations=1000000,
    actor_fc_layers=(256, 256),
    critic_obs_fc_layers=None,
    critic_action_fc_layers=None,
    critic_joint_fc_layers=(256, 256),
    # Params for collect
    initial_collect_steps=10000,
    collect_steps_per_iteration=1,
    replay_buffer_capacity=1000000,
    # Params for target update
    target_update_tau=0.005,
    target_update_period=1,
    # Params for train
    train_steps_per_iteration=1,
    batch_size=256,
    actor_learning_rate=3e-4,
    critic_learning_rate=3e-4,
    alpha_learning_rate=3e-4,
    td_errors_loss_fn=tf.losses.mean_squared_error,
    gamma=0.99,
    reward_scale_factor=1.0,
    gradient_clipping=None,
    # Params for eval
    num_eval_episodes=100,
    eval_interval=500000,
    # Params for summaries and logging
    train_checkpoint_interval=10000,
    policy_checkpoint_interval=5000,
    rb_checkpoint_interval=50000,
    log_interval=10000,
    summary_interval=10000,
    summaries_flush_secs=10,
    debug_summaries=False,
    summarize_grads_and_vars=False,
    eval_metrics_callback=None):

  """A simple train and eval for SAC."""
  root_dir = os.path.expanduser(root_dir)
  train_dir = os.path.join(root_dir, 'train')
  eval_dir = os.path.join(root_dir, 'eval')

  train_summary_writer = tf.contrib.summary.create_file_writer(
      train_dir, flush_millis=summaries_flush_secs * 1000)
  train_summary_writer.set_as_default()

  eval_summary_writer = tf.contrib.summary.create_file_writer(
      eval_dir, flush_millis=summaries_flush_secs * 1000)
  eval_metrics = [
      py_metrics.AverageReturnMetric(buffer_size=num_eval_episodes),
      py_metrics.AverageEpisodeLengthMetric(buffer_size=num_eval_episodes),
  ]

  # TODO(kbanoop): Figure out if it is possible to avoid the with block.
  with tf.contrib.summary.record_summaries_every_n_global_steps(
      summary_interval):

    # Create the environment.
    tf_env = tf_py_environment.TFPyEnvironment(suite_mujoco.load(env_name))
    eval_py_env = suite_mujoco.load(env_name)

    # Get the data specs from the environment
    time_step_spec = tf_env.time_step_spec()
    observation_spec = time_step_spec.observation
    action_spec = tf_env.action_spec()

    actor_net = actor_distribution_network.ActorDistributionNetwork(
        observation_spec, action_spec,
        fc_layer_params=actor_fc_layers,
        continuous_projection_net=_normal_projection_net)
    critic_net = critic_network.CriticNetwork(
        (observation_spec, action_spec),
        observation_fc_layer_params=critic_obs_fc_layers,
        action_fc_layer_params=critic_action_fc_layers,
        joint_fc_layer_params=critic_joint_fc_layers)

    tf_agent = sac_agent.SacAgent(
        time_step_spec,
        action_spec,
        actor_network=actor_net,
        critic_network=critic_net,
        actor_optimizer=tf.train.AdamOptimizer(
            learning_rate=actor_learning_rate),
        critic_optimizer=tf.train.AdamOptimizer(
            learning_rate=critic_learning_rate),
        alpha_optimizer=tf.train.AdamOptimizer(
            learning_rate=alpha_learning_rate),
        target_update_tau=target_update_tau,
        target_update_period=target_update_period,
        td_errors_loss_fn=td_errors_loss_fn,
        gamma=gamma,
        reward_scale_factor=reward_scale_factor,
        gradient_clipping=gradient_clipping,
        debug_summaries=debug_summaries,
        summarize_grads_and_vars=summarize_grads_and_vars)

    # Make the replay buffer.
    replay_buffer = tf_uniform_replay_buffer.TFUniformReplayBuffer(
        data_spec=tf_agent.collect_data_spec(),
        batch_size=1,
        max_length=replay_buffer_capacity)
    replay_observer = [replay_buffer.add_batch]

    eval_py_policy = py_tf_policy.PyTFPolicy(tf_agent.policy())

    train_metrics = [
        tf_metrics.NumberOfEpisodes(),
        tf_metrics.EnvironmentSteps(),
        tf_py_metric.TFPyMetric(py_metrics.AverageReturnMetric()),
        tf_py_metric.TFPyMetric(py_metrics.AverageEpisodeLengthMetric()),
    ]

    global_step = tf.train.get_or_create_global_step()

    collect_policy = tf_agent.collect_policy()
    initial_collect_op = dynamic_step_driver.DynamicStepDriver(
        tf_env,
        collect_policy,
        observers=replay_observer,
        num_steps=initial_collect_steps).run()

    collect_op = dynamic_step_driver.DynamicStepDriver(
        tf_env,
        collect_policy,
        observers=replay_observer + train_metrics,
        num_steps=collect_steps_per_iteration).run()

    # Prepare replay buffer as dataset with invalid transitions filtered.
    def _filter_invalid_transition(trajectories, unused_arg1):
      return ~trajectories.is_boundary()[0]
    dataset = replay_buffer.as_dataset(
        sample_batch_size=5*batch_size, num_steps=2).apply(
            tf.contrib.data.unbatch()).filter(_filter_invalid_transition).batch(
                batch_size).prefetch(batch_size*5)
    dataset_iterator = dataset.make_initializable_iterator()
    trajectories, unused_info = dataset_iterator.get_next()
    train_op = tf_agent.train(trajectories, train_step_counter=global_step)

    for train_metric in train_metrics:
      train_metric.tf_summaries(step_metrics=train_metrics[:2])
    summary_op = tf.contrib.summary.all_summary_ops()

    with eval_summary_writer.as_default(), \
         tf.contrib.summary.always_record_summaries():
      for eval_metric in eval_metrics:
        eval_metric.tf_summaries()

    train_checkpointer = common_utils.Checkpointer(
        ckpt_dir=train_dir,
        agent=tf_agent,
        global_step=global_step,
        metrics=tf.contrib.checkpoint.List(train_metrics))
    policy_checkpointer = common_utils.Checkpointer(
        ckpt_dir=os.path.join(train_dir, 'policy'),
        policy=tf_agent.policy(),
        global_step=global_step)
    rb_checkpointer = common_utils.Checkpointer(
        ckpt_dir=os.path.join(train_dir, 'replay_buffer'),
        max_to_keep=1,
        replay_buffer=replay_buffer)

    with tf.Session() as sess:
      # Initialize graph.
      train_checkpointer.initialize_or_restore(sess)
      policy_checkpointer.initialize_or_restore(sess)
      rb_checkpointer.initialize_or_restore(sess)

      # Initialize training.
      sess.run(dataset_iterator.initializer)
      common_utils.initialize_uninitialized_variables(sess)
      tf.contrib.summary.initialize(session=sess)

      global_step_val = sess.run(global_step)

      if global_step_val == 0:
        # Initial eval of randomly initialized policy
        metric_utils.compute_summaries(
            eval_metrics,
            eval_py_env,
            eval_py_policy,
            num_episodes=num_eval_episodes,
            global_step=global_step_val,
            callback=eval_metrics_callback,
        )

        # Run initial collect.
        tf.logging.info('Global step %d: Running initial collect op.',
                        global_step_val)
        sess.run(initial_collect_op)

        # Checkpoint the initial replay buffer contents.
        rb_checkpointer.save(global_step=global_step_val)

        tf.logging.info('Finished initial collect.')
      else:
        tf.logging.info('Global step %d: Skipping initial collect op.',
                        global_step_val)

      collect_call = sess.make_callable(collect_op)
      train_step_call = sess.make_callable([train_op, summary_op, global_step])

      timed_at_step = sess.run(global_step)
      time_acc = 0
      steps_per_second_ph = tf.placeholder(
          tf.float32, shape=(), name='steps_per_sec_ph')
      steps_per_second_summary = tf.contrib.summary.scalar(
          name='global_steps/sec', tensor=steps_per_second_ph)

      for _ in range(num_iterations):
        start_time = time.time()
        collect_call()
        for _ in range(train_steps_per_iteration):
          total_loss, _, global_step_val = train_step_call()
        time_acc += time.time() - start_time

        if global_step_val % log_interval == 0:
          tf.logging.info('step = %d, loss = %f',
                          global_step_val, total_loss.loss)
          steps_per_sec = (global_step_val - timed_at_step) / time_acc
          tf.logging.info('%.3f steps/sec' % steps_per_sec)
          sess.run(
              steps_per_second_summary,
              feed_dict={steps_per_second_ph: steps_per_sec})
          timed_at_step = global_step_val
          time_acc = 0

        if global_step_val % eval_interval == 0:
          metric_utils.compute_summaries(
              eval_metrics,
              eval_py_env,
              eval_py_policy,
              num_episodes=num_eval_episodes,
              global_step=global_step_val,
              callback=eval_metrics_callback,

          )

        if global_step_val % train_checkpoint_interval == 0:
          train_checkpointer.save(global_step=global_step_val)

        if global_step_val % policy_checkpoint_interval == 0:
          policy_checkpointer.save(global_step=global_step_val)

        if global_step_val % rb_checkpoint_interval == 0:
          rb_checkpointer.save(global_step=global_step_val)


def main(_):
  root_dir = FLAGS.root_dir
  tf.logging.set_verbosity(tf.logging.INFO)
  gin.parse_config_files_and_bindings(FLAGS.config_file, FLAGS.binding)
  train_eval(root_dir,
             num_iterations=FLAGS.num_iterations,
             env_name=FLAGS.env_name)


if __name__ == '__main__':
  flags.mark_flag_as_required('root_dir')
  tf.app.run()
