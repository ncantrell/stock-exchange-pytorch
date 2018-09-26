import numpy as np
import torch
import torch.nn as nn
from reinforcement.environment import device

FEATURES = 128
KERNEL_SIZE = (4, 8)


class Flatten(nn.Module):
    def forward(self, input):
        return input.view(input.size(0), -1)


class DuelingDQN(nn.Module):
    # Eventually, n_tickers should create n number of values and advantages
    # Take env as input, figure out the size from there?
    def __init__(self, n_input_features, n_action_space, n_tickers=None):
        super(DuelingDQN, self).__init__()
        self.n_action_space = n_action_space

        self.feature = nn.Sequential(
            nn.Conv2d(1,        FEATURES, KERNEL_SIZE),
            nn.ReLU(),
            nn.Conv2d(FEATURES, FEATURES, KERNEL_SIZE),
            nn.ReLU(),
            nn.Conv2d(FEATURES, FEATURES, KERNEL_SIZE),
            nn.ReLU(),
            Flatten(),
        )

        self.value = nn.Sequential(
            nn.Linear(43648, 1024),
            nn.ReLU(),
            nn.Linear(1024, 32),
            nn.ReLU(),
            nn.Linear(32, 1)
        )

        self.advantage = nn.Sequential(
            nn.Linear(43648, 1024),
            nn.ReLU(),
            nn.Linear(1024, 32),
            nn.ReLU(),
            nn.Linear(32, n_action_space)
        )

    def forward(self, x):
        # Some thing is wrong... Need to comeback to this...
        if x.dim() == 3:
            x.unsqueeze_(1)

        x = self.feature(x)
        value = self.value(x)
        advantage = self.advantage(x)
        return value + advantage - advantage.mean()

    def predict(self, x):
        return self.forward(x).detach().sort(dim=1, descending=True)

    def act(self, x, epsilon):
        if not torch.is_tensor(x):
            x = torch.tensor([x], dtype=torch.float32, device=device)

        assert x.dim() == 3, 'Somehow, x.shape is:{}'.format(x.shape)
        x.unsqueeze_(1)

        if np.random.rand() > epsilon:
            x = self.feature(x).detach()
            # print(self.advantage(x).detach().sort(dim=1, descending=True))
            x = self.advantage(x).detach().argmax().item()
            return x
        else:
            return np.random.randint(self.n_action_space)

