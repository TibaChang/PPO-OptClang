#!/usr/bin/env python3
"""
The algorithm is based on MorvanZhou's implementation: https://morvanzhou.github.io/tutorials
And he also refers to the work of OpenAI and DeepMind.

Algorithm:
A simple version of OpenAI's Proximal Policy Optimization (PPO). [https://arxiv.org/abs/1707.06347]
Distributing workers in parallel to collect data, then stop worker's roll-out and train PPO on collected data.
Restart workers once PPO is updated.
The global PPO updating rule is adopted from DeepMind's paper (DPPO):
Emergence of Locomotion Behaviours in Rich Environments (Google Deepmind): [https://arxiv.org/abs/1707.02286]

Dependencies:
tensorflow r1.5
gym 0.9.2
gym_OptClang
"""

import tensorflow as tf
import numpy as np
import matplotlib.pyplot as plt
import gym, gym_OptClang
import random, threading, queue, operator, os, sys, re
from operator import itemgetter

EP_MAX = 1000
N_WORKER = 1                # parallel workers
GAMMA = 0.9                 # reward discount factor
A_LR = 0.0001               # learning rate for actor
C_LR = 0.0002               # learning rate for critic
MIN_BATCH_SIZE = 24         # minimum batch size for updating PPO
UPDATE_STEP = 10            # loop update operation n-steps
EPSILON = 0.2               # for clipping surrogate objective


class PPO(object):
    def __init__(self, env):
        tf.reset_default_graph()
        self.S_DIM = len(env.observation_space.low)
        self.A_DIM = env.action_space.n
        self.sess = tf.Session()
        self.tfs = tf.placeholder(tf.float32, [None, self.S_DIM], 'state')

        # critic
        l1 = tf.layers.dense(self.tfs, 100, tf.nn.relu)
        self.v = tf.layers.dense(l1, 1)
        self.tfdc_r = tf.placeholder(tf.float32, [None, 1], 'discounted_r')
        self.advantage = self.tfdc_r - self.v
        self.closs = tf.reduce_mean(tf.square(self.advantage))
        self.ctrain_op = tf.train.AdamOptimizer(C_LR).minimize(self.closs)

        # actor
        pi, pi_params = self._build_anet('pi', trainable=True)
        oldpi, oldpi_params = self._build_anet('oldpi', trainable=False)
        self.sample_op = tf.squeeze(pi.sample(1), axis=0)  # operation of choosing action
        self.update_oldpi_op = [oldp.assign(p) for p, oldp in zip(pi_params, oldpi_params)]

        self.tfa = tf.placeholder(tf.float32, [None, self.A_DIM], 'action')
        self.tfadv = tf.placeholder(tf.float32, [None, 1], 'advantage')
        # ratio = tf.exp(pi.log_prob(self.tfa) - oldpi.log_prob(self.tfa))
        ratio = pi.prob(self.tfa) / (oldpi.prob(self.tfa) + 1e-5)
        surr = ratio * self.tfadv                       # surrogate loss

        self.aloss = -tf.reduce_mean(tf.minimum(        # clipped surrogate objective
            surr,
            tf.clip_by_value(ratio, 1. - EPSILON, 1. + EPSILON) * self.tfadv))

        self.atrain_op = tf.train.AdamOptimizer(A_LR).minimize(self.aloss)
        self.sess.run(tf.global_variables_initializer())

    def update(self, SharedQueue, CollectEvent, UpdateEvent):
        global GLOBAL_UPDATE_COUNTER
        while not COORD.should_stop():
            if GLOBAL_EP < EP_MAX:
                # wait until get batch of data
                UpdateEvent.wait()
                # copy pi to old pi
                self.sess.run(self.update_oldpi_op)
                # collect data from all workers
                data = [SharedQueue.get() for _ in range(SharedQueue.qsize())]
                data = np.vstack(data)
                s, a, r = data[:, :self.S_DIM], data[:, self.S_DIM: self.S_DIM + self.A_DIM], data[:, -1:]
                adv = self.sess.run(self.advantage, {self.tfs: s, self.tfdc_r: r})
                # update actor and critic in a update loop
                [self.sess.run(self.atrain_op, {self.tfs: s, self.tfa: a, self.tfadv: adv}) for _ in range(UPDATE_STEP)]
                [self.sess.run(self.ctrain_op, {self.tfs: s, self.tfdc_r: r}) for _ in range(UPDATE_STEP)]
                UpdateEvent.clear()        # updating finished
                GLOBAL_UPDATE_COUNTER = 0   # reset counter
                CollectEvent.set()         # set collecting available

    def _build_anet(self, name, trainable):
        with tf.variable_scope(name):
            l1 = tf.layers.dense(self.tfs, 200, tf.nn.relu, trainable=trainable)
            mu = 2 * tf.layers.dense(l1, self.A_DIM, tf.nn.tanh, trainable=trainable)
            sigma = tf.layers.dense(l1, self.A_DIM, tf.nn.softplus, trainable=trainable)
            norm_dist = tf.distributions.Normal(loc=mu, scale=sigma)
        params = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope=name)
        return norm_dist, params

    def choose_action(self, s, PassHistory):
        """
        return a int from 0 to 33
        In the world of reinforcement learning, the action space is from 0 to 33.
        However, in the world of modified-clang, the accepted passes are from 1 to 34.
        Therefore, "gym-OptClang" already done this effort for us.
        We don't have to bother this by ourselves.
        """
        s = s[np.newaxis, :]
        a = self.sess.run(self.sample_op, {self.tfs: s})[0]
        '''
        choose the one that was not applied yet
        '''
        # split the probabilities into list of [index ,probablities]
        aList = a.tolist()
        probList = []
        idx = 0
        for prob in aList:
            probList.append([idx, prob])
            idx += 1
        # sort with probs in descending order
        probList.sort(key=itemgetter(1), reverse=True)
        # find the one that is not applied yet
        for actionProb in probList:
            PassIdx = actionProb[0]
            PassProb = actionProb[1]
            if PassIdx not in PassHistory:
                PassHistory[PassIdx] = 'Used'
                return PassIdx
        # the code should never come to here
        return 'Error'

    def get_v(self, s):
        if s.ndim < 2: s = s[np.newaxis, :]
        return self.sess.run(self.v, {self.tfs: s})[0, 0]


class Worker(object):
    def __init__(self, WorkerID, Locks, GAME, Events):
        self.wid = WorkerID
        self.env = gym.make(GAME).unwrapped
        self.ppo = PPO(self.env)
        self.SharedLocks = Locks
        self.SharedEvents = Events

    def getMostInfluentialState(self, states, ResetInfo):
        """
        return the most influential features from profiled data.
        If not profiled, random pick.
        If the function name does not match in the fetures, try others based on the usage
        in descending order.
        return an numpy array object.
        """
        retVec = None
        Stats = ResetInfo["FunctionUsageDict"]
        if not Stats.items():
            '''
            nothing profiled, random select
            '''
            key = random.choice(list(states.keys()))
        else:
            '''
            select the function with the maximum usage
            '''
            key = max(Stats.items(), key=operator.itemgetter(1))[0]
        #key = "hi_function"
        try:
            retVec = states[key]
        except KeyError:
            '''
            Random selection will never come to here.
            This is caused by perf profiled information which does not contain the function arguments.
            '''
            #print("Using re to search C++ style name\nKey error:\nkey={}\ndict.keys()={}\n".format(key, states.keys()))
            try:
                FunctionList = list(states.keys())
                done = False
                # build list of [key, usage]
                UsageList = []
                for name, usage in Stats.items():
                    UsageList.append([name, usage])
                # based on the usage, sort it
                sorted(UsageList, key=operator.itemgetter(1), reverse=True)
                # use RegExp to search C++ style name or ambiguity of arguments.
                NameList = []
                UsageTmpList = []
                done = False
                for item in UsageList:
                    NameList.append(item[0])
                    UsageTmpList.append(item[1])
                for cand in NameList:
                    # searching based on the usage order in descending.
                    realKey = self.RegExpSearch(cand, FunctionList)
                    if realKey is not None:
                        done = True
                        break
                if not done:
                    # if we cannot find the key, use the random one.
                    realKey = random.choice(FunctionList)
                retVec = states[realKey]
            except Exception as e:
                print("Unexpected exception\nkey={}\nrealKey={}\ndict.keys()={}\nreason={}\n".format(key, realKey ,states.keys()), e)
        return np.asarray(retVec)

    def RegExpSearch(self, TargetName, List):
        """
        Use regular exp. to search whether the List contains the TargetName.
        Inputs:
            TargetName: the name you would like to find.
            List: list of candidates for searching.
        Return:
            The matched name in List or None
        """
        retName = None
        done = False
        for candidate in List:
            matched = re.search(re.escape(TargetName), candidate)
            if matched is not None:
                retName = candidate
                done = True
                break
        if not done:
            ReEscapedInput = re.escape(TargetName)
            SearchTarget = ".*{name}.*".format(name=ReEscapedInput)
            r = re.compile(SearchTarget)
            reRetList = list(filter(r.search, List))
            if reRetList:
                retName = reRetList[0]
        return retName

    def calcEachReward(self, newInfo, MeanSigmaDict, Features, oldInfo, oldCycles, FirstEpi=False):
        """
        return dict={"function-name": reward(float)}

        if FirstEpi == True:
            oldInfo will be the ResetInfo
        if FirstEpi == False:
            oldInfo will be the usage dict from last epi.
        """
        Stats = newInfo["FunctionUsageDict"]
        TotalCycles = newInfo["TotalCyclesStat"]
        Target = newInfo["Target"]
        '''
        Generate dict for function name mapping between perf style and clang style
        (Info["FunctionUsageDict"] <--> Features)
        {"perf_style_name": "clang_style_name"}
        '''
        NameMapDict = {}
        AllFunctions = list(Features.keys())
        for perfName in list(Stats.keys()):
            NameMapDict[perfName] = self.RegExpSearch(perfName, AllFunctions)
        '''
        Create usage dict with clang_style_name as key.
        if not profiled, the value will be None
        '''
        newAllUsageDict = {k : None for k in AllFunctions}
        for perf_name, clang_name in NameMapDict.items():
            newAllUsageDict[clang_name] = Stats[perf_name]
        '''
        Prepare the old usage dict
        '''
        if FirstEpi == True:
            resetStats = oldInfo["FunctionUsageDict"]
            resetNameMapDict = {}
            resetAllFunctions = list(Features.keys())
            for perfName in list(resetStats.keys()):
                resetNameMapDict[perfName] = self.RegExpSearch(perfName, resetAllFunctions)
            oldAllUsageDict = {k : None for k in resetAllFunctions}
            for perf_name, clang_name in resetNameMapDict.items():
                oldAllUsageDict[clang_name] = resetStats[perf_name]
        else:
            oldAllUsageDict = oldInfo
        '''
        Calculate real reward based on the (new/old)AllUsageDict and MeanSigmaDict for all functions
        '''
        rewards = {f : None for f in AllFunctions}
        target = newInfo['Target']
        old_total_cycles = oldCycles
        new_total_cycles = TotalCycles
        delta_total_cycles = old_total_cycles - new_total_cycles
        abs_delta_total_cycles = abs(delta_total_cycles)
        sigma_total_cycles = MeanSigmaDict[target]['sigma']
        '''
        95% of results are in the twice sigma.
        Therefore, 2x is necessary.
        '''
        SigmaRatio = abs((abs_delta_total_cycles - sigma_total_cycles)/(2*sigma_total_cycles))
        UsageNumOverAll = 0
        for name, usage in newAllUsageDict.items():
            if usage is not None:
                UsageNumOverAll += 1
        UsageProfiledRatio = UsageNumOverAll/len(newAllUsageDict)
        for FunctionName in AllFunctions:
            old_usage = oldAllUsageDict[FunctionName]
            new_usage = newAllUsageDict[FunctionName]
            UseOverallPerf = False
            '''
            The Alpha and Beta need to be tuned.
            '''
            Alpha = 2
            Beta = 2
            isSpeedup = False
            isSlowDown = False
            if old_usage is None and new_usage is None:
                '''
                This function does not matters
                '''
                UseOverallPerf = True
            elif old_usage is None:
                '''
                may be slow down
                '''
                UseOverallPerf = True
                Alpha *= Beta
                isSlowDown = True
            elif new_usage is None:
                '''
                may be speedup
                '''
                UseOverallPerf = True
                Alpha *= Beta
                isSpeedup = True
            else:
                '''
                This may be more accurate
                How important: based on how many functions are profiled.
                '''
                UseOverallPerf = False
                Alpha = Alpha*(Beta*(1 / UsageProfiledRatio))
            if UseOverallPerf:
                if isSlowDown == True and delta_total_cycles > 0:
                    Alpha /= Beta
                    delta_total_cycles *= -1
                elif isSpeedup == True and delta_total_cycles < 0:
                    Alpha /= Beta
                    delta_total_cycles *= -1
                reward = Alpha*SigmaRatio*(delta_total_cycles/old_total_cycles)
            else:
                old_function_cycles = old_total_cycles * old_usage
                new_function_cycles = new_total_cycles * new_usage
                delta_function_cycles = old_function_cycles - new_function_cycles
                reward = Alpha*SigmaRatio*(delta_function_cycles/old_function_cycles)
            #print("FunctionName={}, reward={}".format(FunctionName, reward))
            #print("Alpha={}".format(Alpha))
            rewards[FunctionName] = reward
        #print("UsageProfiledRatio={}".format(UsageProfiledRatio))
        # return newAllUsageDict to be the "old" for next episode
        return rewards, newAllUsageDict

    def appendStateRewards(self, buffer_s, buffer_a, buffer_r, states, rewards, action):
        #FIXME: do we need to discard some results that the rewards are not that important?
        """
        No return value, they are append inplace in buffer_x
        buffer_s : dict of list of np.array as features
        buffer_a : dict of list of actions(int)
        buffer_r : dict of list of rewards(float)
        """
        for name, featureList in states.items():
            # For some reason, the name may be '' (remove it!)
            if not name:
                buffer_s.pop('', None)
                buffer_a.pop('', None)
                buffer_r.pop('', None)
                continue
            if buffer_s.get(name) is None:
                buffer_s[name] = []
                buffer_a[name] = []
                buffer_r[name] = []
            buffer_s[name].append(np.asarray(featureList, dtype=np.uint32))
            actionFeature = [0]*34
            actionFeature[action] = 1
            buffer_a[name].append(actionFeature)
            buffer_r[name].append(rewards[name])



    def calcDiscountedRewards(self, buffer_r, nextObs):
        """
        return a dict of list of discounted rewards
        {"function-name":[discounted rewards]}
        """
        global GAMMA
        retDict = {}
        for name, FeatureList in nextObs.items():
            '''
            Get estimated rewards from critic
            '''
            nextOb = np.asarray(FeatureList, dtype=np.uint32)
            StateValue = self.ppo.get_v(nextOb)
            discounted_r = []
            for r in buffer_r[name][::-1]:
                '''
                Calculate discounted rewards
                '''
                StateValue = r + GAMMA * StateValue
                discounted_r.append(StateValue)
            discounted_r.reverse()
            retDict[name] = discounted_r
        return retDict


    def calcEpisodeReward(self, rewards):
        """
        return the average reward.
        """
        total = 0.0
        count = 0.0
        for name, reward in rewards.items():
            total += reward
            count += 1.0
        return total / count


    def getCpuMeanSigmaInfo(self):
        """
        return a dict{"target name": {"mean": int, "sigma": int}}
        """
        path = os.getenv('LLVM_THESIS_RandomHome', 'Error')
        if path == 'Error':
            print("$LLVM_THESIS_RandomHome is not defined, exit!", file=sys.stderr)
            sys.exit(1)
        path = path + '/LLVMTestSuiteScript/GraphGen/output/newMeasurableStdBenchmarkMeanAndSigma'
        if not os.path.exists(path):
            print("{} does not exist.".format(path), file=sys.stderr)
            sus.exit(1)
        retDict = {}
        with open(path, 'r') as file:
            for line in file:
                '''
                ex.
                PAQ8p/paq8p; cpu-cycles-mean | 153224947840; cpu-cycles-sigma | 2111212874
                '''
                lineList = line.split(';')
                name = lineList[0].split('/')[-1].strip()
                mean = int(lineList[1].split('|')[-1].strip())
                sigma = int(lineList[2].split('|')[-1].strip())
                retDict[name] = {'mean':mean, 'sigma':sigma}
            file.close()
        return retDict

    def DictToVstack(self, buffer_s, buffer_a, buffer_r):
        """
        return vstack of state, action and rewards.
        """
        list_s = []
        list_a = []
        list_r = []
        for name, values in buffer_s.items():
            list_s.extend(buffer_s[name])
            list_a.extend(buffer_a[name])
            list_r.extend(buffer_r[name])
        return np.vstack(list_s), np.vstack(list_a), np.vstack(list_r)

    def work(self, SharedQueue):
        global GLOBAL_EP, GLOBAL_RUNNING_R, GLOBAL_UPDATE_COUNTER
        while not COORD.should_stop():
            QueueLock = self.SharedLocks['queue']
            CounterLock = self.SharedLocks['counter']
            PlotEpiLock = self.SharedLocks['plot_epi']
            CollectEvent = self.SharedEvents['collect']
            UpdateEvent = self.SharedEvents['update']
            states, ResetInfo = self.env.reset()
            EpisodeReward = 0
            buffer_s, buffer_a, buffer_r = {}, {}, {}
            MeanSigmaDict = self.getCpuMeanSigmaInfo()
            FirstEpi = True
            PassHistory = {}
            while True:
                # while global PPO is updating
                if not CollectEvent.is_set():
                    # wait until PPO is updated
                    CollectEvent.wait()
                    # clear history buffer, use new policy to collect data
                    buffer_s, buffer_a, buffer_r = {}, {}, {}
                '''
                Save the last profiled info to calculate real rewards
                '''
                if FirstEpi:
                    oldCycles = ResetInfo["TotalCyclesStat"]
                    oldInfo = ResetInfo
                    FirstEpi = False
                    isUsageNotProcessed = True
                else:
                    oldCycles = info["TotalCyclesStat"]
                    oldInfo = oldAllUsage
                    isUsageNotProcessed = False
                '''
                Choose the features from the most inflential function
                '''
                state = self.getMostInfluentialState(states, ResetInfo)
                action = self.ppo.choose_action(state, PassHistory)
                nextStates, reward, done, info = self.env.step(action)
                '''
                If build failed, skip it.
                '''
                if reward < 0:
                    break

                '''
                Calculate actual rewards for all functions
                '''
                rewards, oldAllUsage = self.calcEachReward(info,
                        MeanSigmaDict, nextStates, oldInfo,
                        oldCycles, isUsageNotProcessed)

                '''
                Match the states and rewards
                '''
                self.appendStateRewards(buffer_s, buffer_a, buffer_r, states, rewards, action)

                '''
                Calculate overall reward for plotting
                '''
                EpisodeReward = self.calcEpisodeReward(rewards)

                # add the generated results
                CounterLock.acquire()
                GLOBAL_UPDATE_COUNTER += len(nextStates.keys())
                CounterLock.release()
                #FIXME
                if True:
                #if GLOBAL_UPDATE_COUNTER >= MIN_BATCH_SIZE or done:
                    '''
                    Calculate discounted rewards for all functions
                    '''
                    discounted_r = self.calcDiscountedRewards(buffer_r, nextStates)
                    '''
                    Convert dict of list into row-array
                    '''
                    vstack_s, vstack_a, vstack_r = self.DictToVstack(buffer_s, buffer_a, discounted_r)
                    '''
                    Split each of vector and assemble into a queue element.
                    '''
                    QueueLock.acquire()
                    # put data in the shared queue
                    for index, item in enumerate(vstack_s):
                        SharedQueue.put(np.hstack((vstack_s[index], vstack_a[index], vstack_r[index])))
                    QueueLock.release()
                    buffer_s, buffer_a, buffer_r = {}, {}, {}

                    if GLOBAL_UPDATE_COUNTER >= MIN_BATCH_SIZE:
                        CollectEvent.clear()       # stop collecting data
                        UpdateEvent.set()          # globalPPO update

                    if GLOBAL_EP >= EP_MAX:         # stop training
                        COORD.request_stop()
                        break
                if done:
                    # clear history of applied passes
                    PassHistory = {}
                    break
                else:
                    states = nextStates

            # FIXME: the code seems never come to here
            # record reward changes, plot later
            PlotEpiLock.acquire()
            if len(GLOBAL_RUNNING_R) == 0:
                GLOBAL_RUNNING_R.append(EpisodeReward)
            else:
                GLOBAL_RUNNING_R.append(GLOBAL_RUNNING_R[-1]*0.9+EpisodeReward*0.1)
            GLOBAL_EP += 1
            PlotEpiLock.release()
            print('{0:.1f}%'.format(GLOBAL_EP/EP_MAX*100), '|W%i' % self.wid,  '|EpisodeReward: %.2f' % EpisodeReward,)


if __name__ == '__main__':
    Game='OptClang-v0'
    # remove worker file list.
    WorkerListLoc = "/tmp/gym-OptClang-WorkerList"
    if os.path.exists(WorkerListLoc):
        os.remove(WorkerListLoc)

    Events = {}
    Events['update'] = threading.Event()
    Events['update'].clear()            # not update now
    Events['collect'] = threading.Event()
    Events['collect'].set()             # start to collect
    # prevent race condition with 3 locks
    #TODO: release all lock when sigterm
    Locks = {}
    Locks['queue'] = threading.Lock()
    Locks['counter'] = threading.Lock()
    Locks['plot_epi'] = threading.Lock()
    workers = []
    for i in range(N_WORKER):
        workers.append(Worker(WorkerID=i, Locks=Locks, GAME=Game, Events=Events))

    GLOBAL_UPDATE_COUNTER, GLOBAL_EP = 0, 0
    GLOBAL_PPO = PPO(gym.make(Game).unwrapped)
    GLOBAL_RUNNING_R = []
    COORD = tf.train.Coordinator()
    # workers putting data in this queue
    SharedQueue = queue.Queue()
    threads = []
    for worker in workers:          # worker threads
        t = threading.Thread(target=worker.work, args=(SharedQueue,))
        t.start()                   # training
        threads.append(t)
    # add a PPO updating thread
    threads.append(threading.Thread(target=GLOBAL_PPO.update,
        args=(SharedQueue, Events['collect'], Events['update'],)))
    threads[-1].start()
    COORD.join(threads)

    # plot reward change
    plt.plot(np.arange(len(GLOBAL_RUNNING_R)), GLOBAL_RUNNING_R)
    plt.xlabel('Episode'); plt.ylabel('Moving reward'); plt.ion(); plt.show()
